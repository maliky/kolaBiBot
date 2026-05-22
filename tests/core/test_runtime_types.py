from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from kolabi.bot.domain import (
    ConfirmedOrder,
    OrderIdentity,
    OrderReason,
    ExecutionOutcome,
    EggMoveKind,
    HeadState,
    OrderState,
    TailMode,
    TailState,
    classify_confirmed_move,
    classify_confirmed_state,
)
from kolabi.shared.core.runtime_types import (
    AmendReason,
    EnvironmentName,
    ExchangeName,
    OrderRole,
    OrderStatus,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    RuntimeCommand,
    RuntimeCommandKind,
    RuntimeEvent,
    RuntimeEventKind,
    Symbol,
    TriggerKind,
)


def test_head_and_tail_state_follow_canonical_contract() -> None:
    assert HeadState is OrderState
    assert TailState is OrderState
    assert HeadState.SUBMITTED.value == "submitted"
    assert TailState.LIVING.value == "living"
    assert TailMode.FLAPPING.value == "flapping"


def test_runtime_event_and_command_types_are_typed_values() -> None:
    symbol = Symbol("PI_XBTUSD")
    event = RuntimeEvent(
        kind=RuntimeEventKind.ORDER_REQUESTED,
        at=datetime.now(timezone.utc),
        symbol=symbol,
        note="head scheduled",
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=symbol,
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Limit",
        ),
    )

    assert event.kind == RuntimeEventKind.ORDER_REQUESTED
    assert command.kind == RuntimeCommandKind.PLACE
    assert command.reason == "head"


def test_extended_enums_and_tagged_outcome_state() -> None:
    assert TriggerKind.STOP.value == "stop"
    assert AmendReason.TRAILING_UPDATE.value == "trailing_update"
    assert ExchangeName.KRAKEN.value == "kraken"
    assert EnvironmentName.DEMO.value == "demo"
    assert OrderStatus.FILLED.value == "Filled"
    assert classify_confirmed_state(ExecutionOutcome.PLAYED) == HeadState.LIVING


def test_confirmed_move_classification_follows_played_cancel_table() -> None:
    identity = OrderIdentity(pair_name="pair-a", role="head")

    assert (
        classify_confirmed_move(
            ConfirmedOrder(
                identity=identity,
                state=HeadState.NEW,
                reason=OrderReason.NEW_PLACED_ORDER_BY_USER,
                filled_quantity=Decimal("0"),
                total_quantity=Decimal("1"),
            )
        )
        == EggMoveKind.NOT_PLAYED_NOR_CANCELED
    )
    assert (
        classify_confirmed_move(
            ConfirmedOrder(
                identity=identity,
                state=HeadState.FAILED,
                reason=OrderReason.CANCELLED_BY_USER,
                filled_quantity=Decimal("0"),
                total_quantity=Decimal("1"),
            )
        )
        == EggMoveKind.NOT_PLAYED_CANCELED
    )
    assert (
        classify_confirmed_move(
            ConfirmedOrder(
                identity=identity,
                state=HeadState.LIVING,
                reason=OrderReason.PARTIAL_FILL,
                filled_quantity=Decimal("0.5"),
                total_quantity=Decimal("1"),
            )
        )
        == EggMoveKind.PLAYED_NOT_CANCELED
    )
    assert (
        classify_confirmed_move(
            ConfirmedOrder(
                identity=identity,
                state=HeadState.CLOSED,
                reason=OrderReason.FULL_FILL,
                filled_quantity=Decimal("0"),
                total_quantity=Decimal("1"),
            )
        )
        == EggMoveKind.PLAYED_AND_CANCELED
    )
