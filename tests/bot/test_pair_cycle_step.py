from __future__ import annotations

from datetime import datetime, timezone

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    HeadState,
    OrderPairSpec,
    PairCycleState,
    Side,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.pair_cycle import step_pair
from kolabi.shared.core.runtime_types import RuntimeCommandKind


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


def egg_move(kind: EggMoveKind, *, played_quantity: float = 0.0) -> EggMove:
    return EggMove(
        kind=kind,
        occurred_at=datetime.now(timezone.utc),
        symbol="PI_XBTUSD",
        reply={
            "orderID": "OID-1",
            "clOrdID": "CID-1",
            "cumQty": played_quantity,
            "orderQty": 2.0,
        },
    )


def submitted_state() -> PairCycleState:
    return PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.SUBMITTED,
    )


def test_step_pair_head_filled_hooks_tail() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.HEAD_FILLED, played_quantity=2.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_mode == TailState.HOOKED
    assert next_state.played_quantity == 2.0
    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.PLACE


def test_step_pair_head_failed_keeps_tail_latent() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.HEAD_FAILED),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_mode == TailState.LATENT
    assert commands == ()


def test_partial_fill_tail_uses_played_quantity_not_planned_quantity() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.HEAD_PARTIAL_FILL, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_mode == TailState.FLAPPING
    assert next_state.played_quantity == 1.0
    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.AMEND
    assert commands[0].order is not None
    assert float(commands[0].order["orderQty"]) == 1.0


def test_step_pair_zero_fill_cancel_still_hooks_tail() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.HEAD_CANCELED_ZERO_FILL, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_mode == TailState.HOOKED
    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.PLACE


def test_step_pair_canceled_head_after_fill_leaves_flying_tail() -> None:
    next_state, commands = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.HEAD_CANCELED_AFTER_FILL, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_mode == TailState.FLYING
    assert next_state.played_quantity == 1.0
    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.PLACE


def test_failed_head_ignores_later_played_event() -> None:
    failed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.FAILED,
        tail_mode=TailState.LATENT,
        played_quantity=0.0,
    )
    next_state, commands = step_pair(
        failed,
        egg_move(EggMoveKind.HEAD_FILLED, played_quantity=2.0),
    )

    assert next_state == failed
    assert commands == ()


def test_closed_head_ignores_later_partial_fill_event() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_mode=TailState.FLYING,
        played_quantity=1.0,
    )
    next_state, commands = step_pair(
        closed,
        egg_move(EggMoveKind.HEAD_PARTIAL_FILL, played_quantity=1.5),
    )

    assert next_state == closed
    assert commands == ()


def test_duplicate_head_filled_does_not_submit_second_tail() -> None:
    first_state, first_commands = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.HEAD_FILLED, played_quantity=2.0),
    )
    second_state, second_commands = step_pair(
        first_state,
        egg_move(EggMoveKind.HEAD_FILLED, played_quantity=2.0),
    )

    assert len(first_commands) == 1
    assert first_commands[0].kind == RuntimeCommandKind.PLACE
    assert second_state == first_state
    assert second_commands == ()


def test_rest_ack_marks_submitted_only() -> None:
    hooked = PairCycleState(pair=sample_pair(), head_state=HeadState.HOOKED)
    next_state, commands = step_pair(
        hooked,
        egg_move(EggMoveKind.HEAD_SUBMITTED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.SUBMITTED
    assert next_state.tail_mode is None
    assert commands == ()


def test_private_confirmation_required_for_tail_hook() -> None:
    submitted = submitted_state()
    next_state, commands = step_pair(
        submitted,
        egg_move(EggMoveKind.HEAD_ACKNOWLEDGED, played_quantity=0.0),
    )

    assert next_state == submitted
    assert commands == ()
