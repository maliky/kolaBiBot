from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kolabi.bot.domain import HeadSpec, OrderPairSpec, Side, TailSpec, TimeWindow
from kolabi.bot.tail_tracking import (
    TailTrailingConfig,
    initial_tail_trail,
    step_tail_trail,
)


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


def test_sell_tail_does_not_amend_when_reference_moves_down() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(pair, trail, Decimal("98"), now + timedelta(seconds=7))

    assert moved.current_stop_price == trail.current_stop_price


def test_sell_tail_first_unblock_requires_twice_initial_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(pair, trail, Decimal("100.9"), now + timedelta(seconds=7))
    waiting = step_tail_trail(pair, trail, Decimal("102.2"), first_unblocked_at)
    still_waiting = step_tail_trail(
        pair,
        waiting,
        Decimal("102.3"),
        first_unblocked_at + timedelta(seconds=49),
    )
    unblocked = step_tail_trail(
        pair,
        still_waiting,
        Decimal("102.4"),
        first_unblocked_at + timedelta(seconds=50),
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.first_unblocked_at is None
    assert waiting.current_stop_price == trail.current_stop_price
    assert waiting.first_unblocked_at == first_unblocked_at
    assert waiting.last_amended_at is None
    assert still_waiting.current_stop_price == trail.current_stop_price
    assert unblocked.current_stop_price > trail.current_stop_price
    assert unblocked.last_amended_at is not None


def test_sell_tail_first_unblock_wait_adds_max_observed_spread() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(
        pair,
        trail,
        Decimal("101.5"),
        now + timedelta(seconds=7),
        spread=Decimal("1"),
    )
    unblocked = step_tail_trail(
        pair,
        blocked,
        Decimal("102.1"),
        first_unblocked_at,
        spread=Decimal("0.25"),
    )
    moved = step_tail_trail(
        pair,
        unblocked,
        Decimal("102.2"),
        first_unblocked_at + timedelta(seconds=50),
        spread=Decimal("0.25"),
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.max_observed_spread == Decimal("1")
    assert unblocked.current_stop_price == trail.current_stop_price
    assert unblocked.first_unblocked_at == first_unblocked_at
    assert unblocked.max_observed_spread == Decimal("1")
    assert moved.current_stop_price > trail.current_stop_price
    assert moved.max_observed_spread == Decimal("1")


def test_sell_tail_respects_six_second_update_gate() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(first_unblock_delay_seconds=0)
    first = step_tail_trail(
        pair,
        trail,
        Decimal("102.5"),
        now + timedelta(seconds=7),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("104"),
        now + timedelta(seconds=10),
        config=cfg,
    )
    third = step_tail_trail(
        pair,
        second,
        Decimal("104.5"),
        now + timedelta(seconds=14),
        config=cfg,
    )

    assert second.current_stop_price == first.current_stop_price
    assert third.current_stop_price > second.current_stop_price


def test_sell_tail_respects_hysteresis() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(
        first_unblock_delay_seconds=0,
        min_amend_ticks=2,
        min_amend_fraction_of_d0=Decimal("0"),
    )
    first = step_tail_trail(
        pair,
        trail,
        Decimal("102.2"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("102.4"),
        now + timedelta(seconds=14),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    assert second.current_stop_price == first.current_stop_price


def test_buy_tail_does_not_amend_when_reference_moves_up() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)

    moved = step_tail_trail(pair, trail, Decimal("103"), now + timedelta(seconds=7))

    assert moved.current_stop_price == trail.current_stop_price


def test_buy_tail_first_unblock_requires_twice_initial_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(pair, trail, Decimal("99.2"), now + timedelta(seconds=7))
    waiting = step_tail_trail(pair, trail, Decimal("97.7"), first_unblocked_at)
    still_waiting = step_tail_trail(
        pair,
        waiting,
        Decimal("97.6"),
        first_unblocked_at + timedelta(seconds=49),
    )
    unblocked = step_tail_trail(
        pair,
        still_waiting,
        Decimal("97.5"),
        first_unblocked_at + timedelta(seconds=50),
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.first_unblocked_at is None
    assert waiting.current_stop_price == trail.current_stop_price
    assert waiting.first_unblocked_at == first_unblocked_at
    assert waiting.last_amended_at is None
    assert still_waiting.current_stop_price == trail.current_stop_price
    assert unblocked.current_stop_price < trail.current_stop_price
    assert unblocked.last_amended_at is not None


def test_buy_tail_first_unblock_wait_adds_max_observed_spread() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)
    first_unblocked_at = now + timedelta(seconds=14)

    blocked = step_tail_trail(
        pair,
        trail,
        Decimal("98.5"),
        now + timedelta(seconds=7),
        spread=Decimal("1"),
    )
    unblocked = step_tail_trail(
        pair,
        blocked,
        Decimal("97.9"),
        first_unblocked_at,
        spread=Decimal("0.25"),
    )
    moved = step_tail_trail(
        pair,
        unblocked,
        Decimal("97.8"),
        first_unblocked_at + timedelta(seconds=50),
        spread=Decimal("0.25"),
    )

    assert blocked.current_stop_price == trail.current_stop_price
    assert blocked.max_observed_spread == Decimal("1")
    assert unblocked.current_stop_price == trail.current_stop_price
    assert unblocked.first_unblocked_at == first_unblocked_at
    assert unblocked.max_observed_spread == Decimal("1")
    assert moved.current_stop_price < trail.current_stop_price
    assert moved.max_observed_spread == Decimal("1")


def test_buy_tail_respects_six_second_update_gate() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(first_unblock_delay_seconds=0)
    first = step_tail_trail(
        pair,
        trail,
        Decimal("97.5"),
        now + timedelta(seconds=7),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("96.5"),
        now + timedelta(seconds=10),
        config=cfg,
    )
    third = step_tail_trail(
        pair,
        second,
        Decimal("96.0"),
        now + timedelta(seconds=14),
        config=cfg,
    )

    assert second.current_stop_price == first.current_stop_price
    assert third.current_stop_price < second.current_stop_price


def test_buy_tail_respects_hysteresis() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(
        first_unblock_delay_seconds=0,
        min_amend_ticks=2,
        min_amend_fraction_of_d0=Decimal("0"),
    )
    first = step_tail_trail(
        pair,
        trail,
        Decimal("97.7"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("97.6"),
        now + timedelta(seconds=14),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    assert second.current_stop_price == first.current_stop_price


def test_tick_rounding_prevents_subtick_noop_amend() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(first_unblock_delay_seconds=0)
    first = step_tail_trail(
        pair,
        trail,
        Decimal("102.2"),
        now + timedelta(seconds=7),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    second = step_tail_trail(
        pair,
        first,
        Decimal("102.2001"),
        now + timedelta(seconds=14),
        tick_size=Decimal("0.5"),
        config=cfg,
    )
    assert second.current_stop_price == first.current_stop_price


def test_tail_tracking_handles_mixed_naive_and_aware_timestamps() -> None:
    aware = datetime.now(timezone.utc)
    naive = aware.replace(tzinfo=None)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), aware)
    cfg = TailTrailingConfig(first_unblock_delay_seconds=0)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("102.2"),
        naive + timedelta(seconds=7),
        config=cfg,
    )
    moved_again = step_tail_trail(
        pair,
        moved,
        Decimal("103.2"),
        aware + timedelta(seconds=14),
        config=cfg,
    )

    assert moved_again.current_stop_price >= moved.current_stop_price


def test_tail_tracking_survives_extreme_reference_without_overflow() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY)
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(first_unblock_delay_seconds=0)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("999999"),
        now + timedelta(seconds=7),
        config=cfg,
    )

    assert moved.current_stop_price >= trail.current_stop_price


def test_sell_tail_lag_is_capped_to_twice_initial_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.BUY, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(first_unblock_delay_seconds=0)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("130"),
        now + timedelta(seconds=7),
        config=cfg,
    )
    lag = Decimal("130") - moved.current_stop_price

    assert lag <= Decimal("2")


def test_buy_tail_lag_is_capped_to_twice_initial_distance() -> None:
    now = datetime.now(timezone.utc)
    pair = sample_pair(side=Side.SELL, tail=1.0, tail_type="tD")
    trail = initial_tail_trail(pair, Decimal("100"), now)
    cfg = TailTrailingConfig(first_unblock_delay_seconds=0)

    moved = step_tail_trail(
        pair,
        trail,
        Decimal("70"),
        now + timedelta(seconds=7),
        config=cfg,
    )
    lag = moved.current_stop_price - Decimal("70")

    assert lag <= Decimal("2")
