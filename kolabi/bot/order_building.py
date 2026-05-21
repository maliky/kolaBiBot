"""Pure order payload builders for the pair-cycle reducer.

Purpose: construct head/tail payloads and typed runtime commands from immutable
pair state without touching exchange clients.
Inputs: `OrderPairSpec`, `PairCycleState`, and reducer context.
Outputs: typed `OrderDict` payloads and `RuntimeCommand` values.
Side effects: none.
Important types: `PairCycleState`, `OrderDict`, `RuntimeCommand`.
Role: pure logic.
Transitional: yes, extracted from `pair_cycle.py` while legacy shells remain.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import cast

from kolabi.bot.domain import OrderPairSpec, PairCycleState, Side, opposite_side
from kolabi.shared.core.runtime_types import (
    OrderDict,
    OrderQty,
    OrderRole,
    PriceOffset,
    RuntimeCommand,
    RuntimeCommandKind,
    StopPrice,
    Symbol,
    to_decimal,
)

def head_order_dict(pair: OrderPairSpec, *, client_order_id: str | None = None) -> OrderDict:
    """Build the head order payload from the static pair specification."""
    order: OrderDict = {
        "side": pair.head.side.value,
        "ordType": pair.head.order_type,
    }
    if pair.head_quantity is not None:
        order["orderQty"] = cast(OrderQty, to_decimal(pair.head_quantity))
    if client_order_id is not None:
        order["clOrdID"] = client_order_id
    return order


def resolve_tail_quantity(state: PairCycleState) -> Decimal | int | None:
    """Resolve tail quantity from played runtime state first, then planned size."""
    if state.played_quantity is not None and state.played_quantity > 0:
        return state.played_quantity
    return state.pair.head_quantity


def tail_order_dict(state: PairCycleState) -> OrderDict:
    """Build a tail payload using runtime played quantity when available."""
    pair = state.pair
    order: OrderDict = {
        "side": opposite_side(pair.head.side).value,
        "ordType": pair.tail.order_type,
    }
    quantity = resolve_tail_quantity(state)
    if quantity is not None:
        order["orderQty"] = cast(OrderQty, to_decimal(quantity))
    if pair.tail_price_spec is not None:
        order["stopPx"] = cast(StopPrice, to_decimal(pair.tail_price_spec))
    if pair.tail.delta is not None:
        order["oDelta"] = cast(PriceOffset, to_decimal(pair.tail.delta))
    return order


def tail_command(
    state: PairCycleState,
    *,
    symbol: Symbol,
    kind: RuntimeCommandKind,
) -> RuntimeCommand:
    """Build a tail command for placement or amendment from runtime state."""
    return RuntimeCommand(
        kind=kind,
        symbol=symbol,
        order=tail_order_dict(state),
        reason=OrderRole.TAIL.value,
    )


def with_played_quantity(state: PairCycleState, quantity: Decimal) -> PairCycleState:
    """Return updated pair state with a normalized played quantity."""
    return replace(state, played_quantity=max(quantity, Decimal("0")))
