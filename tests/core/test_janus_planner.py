from __future__ import annotations

from decimal import Decimal
from typing import cast

import pytest

from kolabi.bot.domain import (
    HeadSpec,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    PairCycleState,
    PairIntent,
    PairIntentKind,
    Side,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.horus import plan_runtime_commands
from kolabi.shared.core.runtime_types import (
    AmendOrderCommandRequest,
    AmendTailCommand,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)


def sample_pair(name: str, *, tail_order_type: str = "Stop") -> OrderPairSpec:
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
        tail=TailSpec(side=Side.SELL, order_type=tail_order_type, delta=0.5),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qApD",
    )


def sample_state(
    *,
    name: str = "pair-a",
    head_state: HeadState = HeadState.LIVING,
    tail_state: TailState | None = TailState.LIVING,
    played_quantity: Decimal | None = Decimal("2"),
    tail_identity: OrderIdentity | None = None,
) -> PairCycleState:
    return PairCycleState(
        pair=sample_pair(name),
        head_state=head_state,
        tail_state=tail_state,
        played_quantity=played_quantity,
        tail_identity=tail_identity,
    )


def test_plan_runtime_commands_preserves_intent_order() -> None:
    state = sample_state()

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


def test_plan_runtime_commands_is_deterministic() -> None:
    state = sample_state()
    intents = (
        PairIntent(PairIntentKind.PLACE_HEAD),
        PairIntent(PairIntentKind.PLACE_TAIL),
    )

    first = plan_runtime_commands(state, intents, symbol=Symbol("PI_XBTUSD"))
    second = plan_runtime_commands(state, intents, symbol=Symbol("PI_XBTUSD"))

    assert first == second


def test_place_head_translates_to_one_head_place_command() -> None:
    commands = plan_runtime_commands(
        sample_state(),
        (PairIntent(PairIntentKind.PLACE_HEAD),),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert len(commands) == 1
    assert isinstance(commands[0], PlaceHeadCommand)
    assert commands[0].kind == RuntimeCommandKind.PLACE
    assert commands[0].reason == "head"
    assert commands[0].request == PlaceOrderCommandRequest(
        pair_name="pair-a",
        side="buy",
        ordType="Limit",
        orderQty=Decimal("2"),
    )


def test_place_tail_translates_to_one_tail_place_command() -> None:
    commands = plan_runtime_commands(
        sample_state(),
        (PairIntent(PairIntentKind.PLACE_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert len(commands) == 1
    assert isinstance(commands[0], PlaceTailCommand)
    assert commands[0].kind == RuntimeCommandKind.PLACE
    assert commands[0].reason == "tail"
    assert commands[0].request == PlaceOrderCommandRequest(
        pair_name="pair-a",
        side="sell",
        ordType="Stop",
        orderQty=Decimal("2"),
        stopPx=Decimal("99.0"),
        oDelta=Decimal("0.5"),
    )


def test_amend_tail_with_full_identity_translates_to_one_amend_command() -> None:
    state = sample_state(
        played_quantity=Decimal("1"),
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        )
    )

    commands = plan_runtime_commands(
        state,
        (PairIntent(PairIntentKind.AMEND_TAIL),),
        symbol=Symbol("PI_XBTUSD"),
    )

    assert len(commands) == 1
    assert isinstance(commands[0], AmendTailCommand)
    assert commands[0].kind == RuntimeCommandKind.AMEND
    assert commands[0].reason == "tail"
    assert commands[0].request == AmendOrderCommandRequest(
        pair_name="pair-a",
        side="sell",
        ordType="Stop",
        orderID="OID-T",
        clOrdID="CID-T",
        newPrice=Decimal("99.0"),
        newQty=Decimal("1"),
    )


def test_amend_tail_without_identity_raises() -> None:
    state = sample_state(
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        played_quantity=Decimal("1"),
    )

    with pytest.raises(ValueError, match="existing tail identity"):
        plan_runtime_commands(
            state,
            (PairIntent(PairIntentKind.AMEND_TAIL),),
            symbol=Symbol("PI_XBTUSD"),
        )


def test_amend_tail_with_partial_identity_raises() -> None:
    state = sample_state(
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id=None,
        )
    )

    with pytest.raises(ValueError, match="both client and exchange order IDs"):
        plan_runtime_commands(
            state,
            (PairIntent(PairIntentKind.AMEND_TAIL),),
            symbol=Symbol("PI_XBTUSD"),
        )


def test_unsupported_intent_kind_raises() -> None:
    bogus_intent = PairIntent(cast(PairIntentKind, "bogus_intent"))

    with pytest.raises(ValueError, match="unsupported pair intent kind"):
        plan_runtime_commands(
            sample_state(),
            (bogus_intent,),
            symbol=Symbol("PI_XBTUSD"),
        )


def test_planner_does_not_mutate_input_state_or_intents() -> None:
    state = sample_state(
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        )
    )
    intents = (
        PairIntent(PairIntentKind.PLACE_HEAD),
        PairIntent(PairIntentKind.AMEND_TAIL),
    )
    state_before = state
    intents_before = intents

    _ = plan_runtime_commands(
        state,
        intents,
        symbol=Symbol("PI_XBTUSD"),
    )

    assert state == state_before
    assert intents == intents_before
