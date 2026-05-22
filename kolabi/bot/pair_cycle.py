"""Pure pair-cycle reducer for the active bot runtime.

Purpose: evaluate one head/tail pair lifecycle through typed reducer moves and
emit ordered command intents without side effects.
Inputs: `PairCycleState` and one typed `EggMove`.
Outputs: next immutable `PairCycleState` and ordered `PairIntent` values.
Side effects: none.
Important types: `PairCycleState`, `EggMove`, `PairIntent`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import cast

from kolabi.bot.domain import (
    classify_confirmed_move,
    ConfirmedOrder,
    EggMove,
    EggMoveKind,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    PairIntent,
    PairIntentKind,
    OrderReason,
    PairCycleState,
    TailMode,
    TailState,
)
from kolabi.bot.dragon import reason_from_status_or_reason
from kolabi.shared.core.runtime_types import to_decimal


def step_pair(
    state: PairCycleState,
    move: EggMove,
) -> tuple[PairCycleState, tuple[PairIntent, ...]]:
    """Pure reducer for one pair transition.

    A reducer maps current immutable state plus one typed move to next immutable
    state and emitted command intents, with no side effects.
    """
    if state.head_state in {HeadState.FAILED, HeadState.CLOSED}:
        return state, ()

    if move.kind == EggMoveKind.HEAD_HOOKED:
        if state.head_state != HeadState.LATENT:
            return state, ()
        next_state = replace(state, head_state=HeadState.HOOKED)
        return next_state, (PairIntent(PairIntentKind.PLACE_HEAD),)

    if move.kind == EggMoveKind.HEAD_SUBMITTED:
        next_state = replace(
            state,
            head_state=HeadState.SUBMITTED,
            head_identity=head_identity_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.NOT_PLAYED_NOR_CANCELED:
        next_state = replace(
            state,
            head_state=HeadState.NEW,
            head_identity=head_identity_from_move(state, move),
            played_quantity=played_quantity_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.NOT_PLAYED_CANCELED:
        next_state = replace(
            state,
            head_state=HeadState.FAILED,
            head_identity=head_identity_from_move(state, move),
            tail_state=TailState.LATENT,
            tail_mode=None,
            played_quantity=played_quantity_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.PLAYED_NOT_CANCELED:
        played_quantity = played_quantity_from_move(state, move)
        if (
            state.head_state == HeadState.LIVING
            and state.played_quantity == played_quantity
            and state.tail_state in {TailState.SUBMITTED, TailState.LIVING}
        ):
            return state, ()
        next_state = replace(
            state,
            head_state=HeadState.LIVING,
            head_identity=head_identity_from_move(state, move),
            tail_state=TailState.LIVING,
            tail_mode=TailMode.FLAPPING,
            played_quantity=played_quantity,
        )
        return next_state, (_tail_intent_for_state(next_state),)

    if move.kind == EggMoveKind.PLAYED_AND_CANCELED:
        played_quantity = played_quantity_from_move(state, move)
        if (
            state.head_state == HeadState.CLOSED
            and state.played_quantity == played_quantity
            and state.tail_mode == TailMode.FLYING
        ):
            return state, ()
        next_state = replace(
            state,
            head_state=HeadState.CLOSED,
            head_identity=head_identity_from_move(state, move),
            tail_state=_next_closed_tail_state(state),
            tail_mode=TailMode.FLYING,
            played_quantity=played_quantity,
        )
        return next_state, (_tail_intent_for_state(next_state),)

    return state, ()


def _tail_intent_for_state(state: PairCycleState) -> PairIntent:
    if state.tail_identity is not None or state.tail_state in {
        TailState.SUBMITTED,
        TailState.LIVING,
    }:
        return PairIntent(PairIntentKind.AMEND_TAIL)
    return PairIntent(PairIntentKind.PLACE_TAIL)


def _next_closed_tail_state(state: PairCycleState) -> TailState:
    """Choisit l'etat du tail ferme selon son activation precedente."""
    if state.tail_state in {TailState.SUBMITTED, TailState.LIVING}:
        return TailState.LIVING
    return TailState.HOOKED


def resolve_quantity(pair: OrderPairSpec) -> float:
    quantity = pair.head_quantity
    if quantity is None or quantity <= 0:
        raise ValueError(f"Order pair '{pair.name}' needs a positive head quantity")
    return float(to_decimal(quantity))


def head_identity_from_move(
    state: PairCycleState,
    move: EggMove,
) -> OrderIdentity | None:
    reply = move.reply or {}
    order = move.order or {}
    order_id = reply.get("orderID")
    client_order_id = reply.get("clOrdID") or order.get("clOrdID")
    if order_id is None and client_order_id is None:
        return state.head_identity
    return OrderIdentity(
        pair_name=state.pair.name,
        role="head",
        client_order_id=str(client_order_id) if client_order_id is not None else None,
        exchange_order_id=str(order_id) if order_id is not None else None,
    )


def played_quantity_from_move(state: PairCycleState, move: EggMove) -> Decimal | None:
    """Read played quantity from move payload and return unknown when missing."""
    reply = move.reply or {}
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = reply.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            parsed = to_decimal(value)
            return parsed if parsed >= Decimal("0") else Decimal("0")
    return None


def egg_move_from_confirmed_head(
    pair: OrderPairSpec,
    head: ConfirmedOrder,
    *,
    symbol: str,
) -> EggMove:
    return EggMove(
        kind=classify_confirmed_move(head),
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        reply={
            "orderID": head.identity.exchange_order_id or "",
            "clOrdID": head.identity.client_order_id or "",
            "ordStatus": head.state.value,
            "execType": head.reason.value,
            "cumQty": float(head.filled_quantity),
            "orderQty": float(head.total_quantity),
        },
    )


def reason_from_status(status: str) -> OrderReason:
    return reason_from_status_or_reason(status, None)
