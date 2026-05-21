from __future__ import annotations

from datetime import datetime, timezone

from kolabi.bot.domain import (
    ExecutionOutcome,
    HeadState,
    OrderState,
    TailMode,
    TailState,
    classify_confirmed_state,
)
from kolabi.shared.core.runtime_types import (
    AmendReason,
    EnvironmentName,
    ExchangeName,
    OrderRole,
    OrderStatus,
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
    command = RuntimeCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=symbol,
        reason=OrderRole.HEAD.value,
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
