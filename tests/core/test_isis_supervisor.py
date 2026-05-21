from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    OrderPairSpec,
    PairCycleState,
    Side,
    StrategyState,
    TailSpec,
    TimeWindow,
)
from kolabi.bot.isis import step_strategy
from kolabi.shared.core.runtime_types import RuntimeCommand


def sample_pair(name: str) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=-1.0, end_minutes=10.0),
        try_num=1,
        dr_pause=None,
        timeout=60,
        head=HeadSpec(side=Side.BUY, order_type="Limit"),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=1,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type="Stop", delta=0.5),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qApD",
    )


def sample_state() -> StrategyState:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    return StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-1",
        pairs={
            "pair-a": PairCycleState(pair=sample_pair("pair-a")),
            "pair-b": PairCycleState(pair=sample_pair("pair-b")),
            "pair-c": PairCycleState(pair=sample_pair("pair-c")),
        },
    )


def test_step_strategy_routes_event_to_single_pair() -> None:
    state = sample_state()
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-1",
    )

    next_state, commands = step_strategy(state, move)

    assert next_state.pairs["pair-a"] == state.pairs["pair-a"]
    assert next_state.pairs["pair-c"] == state.pairs["pair-c"]
    assert next_state.pairs["pair-b"].head_state.value == "hooked"
    assert len(commands) == 1
    assert isinstance(commands[0], RuntimeCommand)
    assert commands[0].order is not None
    assert commands[0].order["pair_name"] == "pair-b"


def test_step_strategy_preserves_other_pairs() -> None:
    state = sample_state()
    baseline_a = state.pairs["pair-a"]
    baseline_c = state.pairs["pair-c"]
    move = EggMove(
        kind=EggMoveKind.HEAD_SUBMITTED,
        occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-2",
        reply={"clOrdID": "CID-B", "orderID": "OID-B"},
    )

    next_state, _ = step_strategy(state, move)

    assert next_state.pairs["pair-a"] is baseline_a
    assert next_state.pairs["pair-c"] is baseline_c
    assert next_state.pairs["pair-b"].head_state.value == "submitted"
    assert next_state.last_event_id == "evt-2"


def test_step_strategy_updates_last_event_metadata() -> None:
    state = sample_state()
    occurred_at = datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc)
    move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=occurred_at,
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-3",
        is_private=True,
    )

    next_state, _ = step_strategy(state, move)
    pair_state = next_state.pairs["pair-b"]

    assert next_state.last_event_id == "evt-3"
    assert next_state.last_event_ts == occurred_at
    assert pair_state.last_processed_private_event_id == "evt-3"
    assert pair_state.last_processed_private_event_ts == occurred_at
    assert pair_state.pair_id == "pair-b"


def test_step_strategy_returns_typed_runtime_commands_only() -> None:
    state = sample_state()
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 4, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        event_id="evt-4",
    )

    _, commands = step_strategy(state, move)

    assert commands
    assert all(isinstance(command, RuntimeCommand) for command in commands)
