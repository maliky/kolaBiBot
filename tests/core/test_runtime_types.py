from __future__ import annotations

from datetime import datetime, timezone

from kolabi.bot.domain import HeadState, OrderState, TailMode, TailState
from kolabi.shared.core.runtime_types import (
    OrderRole,
    RuntimeCommand,
    RuntimeCommandKind,
    RuntimeEvent,
    RuntimeEventKind,
    Symbol,
)


def test_head_and_tail_state_preserve_legacy_aliases() -> None:
    assert HeadState is OrderState
    assert TailState is TailMode
    assert HeadState.SUBMITTED.value == "submitted"
    assert TailState.FLAPPING.value == "flapping"


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
        reason=OrderRole.PRIMARY.value,
    )

    assert event.kind == RuntimeEventKind.ORDER_REQUESTED
    assert command.kind == RuntimeCommandKind.PLACE
    assert command.reason == "primary"
