from __future__ import annotations

from decimal import Decimal

from kolabi.bot.domain import (
    HeadSpec,
    HeadState,
    OrderPairSpec,
    PairCycleState,
    PairIntent,
    PairIntentKind,
    Side,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.janus import plan_runtime_commands
from kolabi.shared.core.runtime_types import RuntimeCommandKind, Symbol


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
        head_quantity=2,
        head_quantity_type="qA",
        tail=TailSpec(side=Side.SELL, order_type="Stop", delta=0.5),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qApD",
    )


def test_plan_runtime_commands_preserves_intent_order() -> None:
    state = PairCycleState(
        pair=sample_pair("pair-a"),
        head_state=HeadState.LIVING,
        tail_state=TailState.LIVING,
        played_quantity=Decimal("2"),
    )

    commands = plan_runtime_commands(
        state,
        (
            PairIntent(PairIntentKind.PLACE_HEAD),
            PairIntent(PairIntentKind.PLACE_TAIL),
        ),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert [command.kind for command in commands] == [
        RuntimeCommandKind.PLACE,
        RuntimeCommandKind.PLACE,
    ]
    assert [command.reason for command in commands] == ["head", "tail"]


def test_plan_runtime_commands_downgrades_tail_amend_without_identity() -> None:
    state = PairCycleState(
        pair=sample_pair("pair-b"),
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        played_quantity=Decimal("1"),
    )

    commands = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.AMEND_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert len(commands) == 1
    assert commands[0].kind == RuntimeCommandKind.PLACE
    assert commands[0].reason == "tail"
