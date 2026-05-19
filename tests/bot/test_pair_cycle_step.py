from __future__ import annotations

from datetime import datetime, timezone

from kolabi.bot.domain import (
    HeadSpec,
    HeadState,
    OrderPairSpec,
    PairCycleState,
    Side,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.pair_cycle import (
    PAIR_EVENT_HEAD_CANCELED,
    PAIR_EVENT_HEAD_FAILED,
    PAIR_EVENT_HEAD_PARTIAL_FILL,
    PAIR_EVENT_HEAD_PLAYED,
    step_pair,
)
from kolabi.shared.core.runtime_types import RuntimeCommandKind, RuntimeEvent, RuntimeEventKind, Symbol


def sample_pair() -> OrderPairSpec:
    return OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=-1.0, end_minutes=1.0),
        attempts=1,
        pause_minutes=None,
        timeout_minutes=None,
        head=HeadSpec(
            side=Side.BUY,
            order_type="Limit",
            price_interval=(100.0, 101.0),
            quantity=2,
            delta=None,
        ),
        tail=TailSpec(
            order_type="Stop",
            price=99.0,
            delta=0.5,
        ),
        amount_type="qApD",
    )


def runtime_event(note: str, *, played_quantity: float = 0.0) -> RuntimeEvent:
    return RuntimeEvent(
        kind=RuntimeEventKind.ORDER_VALIDATED,
        at=datetime.now(timezone.utc),
        symbol=Symbol("PI_XBTUSD"),
        reply={
            "orderID": "OID-1",
            "clOrdID": "CID-1",
            "cumQty": played_quantity,  # type: ignore[typeddict-unknown-key]
            "orderQty": 2.0,  # type: ignore[typeddict-unknown-key]
        },
        note=note,
    )


def submitted_state() -> PairCycleState:
    return PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.SUBMITTED,
    )


def test_step_pair_head_played_hooks_tail() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        runtime_event(PAIR_EVENT_HEAD_PLAYED, played_quantity=2.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_mode == TailState.HOOKED
    assert next_state.played_quantity == 2.0
    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.PLACE


def test_step_pair_head_failed_keeps_tail_latent() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        runtime_event(PAIR_EVENT_HEAD_FAILED),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_mode == TailState.LATENT
    assert commands == ()


def test_step_pair_partial_fill_sets_flapping_tail() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        runtime_event(PAIR_EVENT_HEAD_PARTIAL_FILL, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_mode == TailState.FLAPPING
    assert next_state.played_quantity == 1.0
    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.PLACE


def test_step_pair_canceled_head_without_play_keeps_tail_latent() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        runtime_event(PAIR_EVENT_HEAD_CANCELED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_mode == TailState.LATENT
    assert commands == ()


def test_step_pair_canceled_head_after_play_leaves_flying_tail() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        runtime_event(PAIR_EVENT_HEAD_CANCELED, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_mode == TailState.FLYING
    assert next_state.played_quantity == 1.0
    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.PLACE
