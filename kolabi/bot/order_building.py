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
    AmendHeadCommand,
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelOrderCommandRequest,
    OrderDict,
    OrderQty,
    OrderRole,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    PriceOffset,
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
        "pair_name": pair.name,
    }
    if pair.head_quantity is not None:
        order["orderQty"] = cast(OrderQty, to_decimal(pair.head_quantity))
    if client_order_id is not None:
        order["clOrdID"] = client_order_id
    return order


def head_place_request(
    pair: OrderPairSpec,
    *,
    client_order_id: str | None = None,
) -> PlaceOrderCommandRequest:
    quantity = None if pair.head_quantity is None else cast(OrderQty, to_decimal(pair.head_quantity))
    return PlaceOrderCommandRequest(
        pair_name=pair.name,
        side=pair.head.side.value,
        ordType=pair.head.order_type,
        orderQty=quantity,
        clOrdID=client_order_id,
    )


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
        "pair_name": pair.name,
    }
    quantity = resolve_tail_quantity(state)
    if quantity is not None:
        order["orderQty"] = cast(OrderQty, to_decimal(quantity))
    stop_price = tail_stop_price(state)
    if stop_price is not None:
        order["stopPx"] = cast(StopPrice, to_decimal(stop_price))
    if pair.tail.delta is not None:
        order["oDelta"] = cast(PriceOffset, to_decimal(pair.tail.delta))
    return order


def tail_place_request(state: PairCycleState) -> PlaceOrderCommandRequest:
    quantity = resolve_tail_quantity(state)
    stop_price = tail_stop_price(state)
    return PlaceOrderCommandRequest(
        pair_name=state.pair.name,
        side=opposite_side(state.pair.head.side).value,
        ordType=state.pair.tail.order_type,
        orderQty=None if quantity is None else cast(OrderQty, to_decimal(quantity)),
        stopPx=None if stop_price is None else cast(StopPrice, to_decimal(stop_price)),
        oDelta=(
            None
            if state.pair.tail.delta is None
            else cast(PriceOffset, to_decimal(state.pair.tail.delta))
        ),
    )


def tail_amend_order_dict(state: PairCycleState) -> OrderDict:
    """Build an amend payload from runtime tail identity and target price."""
    if state.tail_identity is None:
        raise ValueError("tail amend requires an existing tail identity")
    if not state.tail_identity.client_order_id or not state.tail_identity.exchange_order_id:
        raise ValueError("tail amend requires both client and exchange order IDs")
    stop_price = tail_stop_price(state)
    if stop_price is None:
        raise ValueError("tail amend requires a planned tail price")

    order = tail_order_dict(state)
    order["clOrdID"] = state.tail_identity.client_order_id
    order["orderID"] = state.tail_identity.exchange_order_id
    order["newPrice"] = cast(StopPrice, to_decimal(stop_price))
    quantity = resolve_tail_quantity(state)
    if quantity is not None:
        order["newQty"] = cast(OrderQty, to_decimal(quantity))
    return order


def tail_amend_request(state: PairCycleState) -> AmendOrderCommandRequest:
    if state.tail_identity is None:
        raise ValueError("tail amend requires an existing tail identity")
    if not state.tail_identity.client_order_id or not state.tail_identity.exchange_order_id:
        raise ValueError("tail amend requires both client and exchange order IDs")
    stop_price = tail_stop_price(state)
    if stop_price is None:
        raise ValueError("tail amend requires a planned tail price")
    quantity = resolve_tail_quantity(state)
    return AmendOrderCommandRequest(
        pair_name=state.pair.name,
        side=opposite_side(state.pair.head.side).value,
        ordType=state.pair.tail.order_type,
        orderID=state.tail_identity.exchange_order_id,
        clOrdID=state.tail_identity.client_order_id,
        newPrice=cast(StopPrice, to_decimal(stop_price)),
        newQty=None if quantity is None else cast(OrderQty, to_decimal(quantity)),
    )


def head_amend_request(state: PairCycleState) -> AmendOrderCommandRequest:
    if state.head_identity is None:
        raise ValueError("head amend requires an existing head identity")
    if not state.head_identity.exchange_order_id:
        raise ValueError("head amend requires an exchange order ID")
    return AmendOrderCommandRequest(
        pair_name=state.pair.name,
        side=state.pair.head.side.value,
        ordType=state.pair.head.order_type,
        orderID=state.head_identity.exchange_order_id,
        clOrdID=state.head_identity.client_order_id,
        text="head amend",
    )


def head_amend_order_dict(state: PairCycleState) -> OrderDict:
    request = head_amend_request(state)
    order: OrderDict = {
        "pair_name": request.pair_name,
        "ordType": request.ordType,
        "side": request.side,
        "orderID": request.orderID,
    }
    if request.clOrdID is not None:
        order["clOrdID"] = request.clOrdID
    if request.text is not None:
        order["text"] = request.text
    return order


def head_command(
    state: PairCycleState,
    *,
    symbol: Symbol,
    kind: RuntimeCommandKind,
) -> PlaceHeadCommand | AmendHeadCommand:
    if kind == RuntimeCommandKind.AMEND:
        request = head_amend_request(state)
        return AmendHeadCommand(
            kind=kind,
            symbol=symbol,
            request=request,
            pair_name=state.pair.name,
            legacy_order=head_amend_order_dict(state),
        )
    return PlaceHeadCommand(
        kind=kind,
        symbol=symbol,
        request=head_place_request(state.pair),
        pair_name=state.pair.name,
        legacy_order=head_order_dict(state.pair),
    )


def tail_command(
    state: PairCycleState,
    *,
    symbol: Symbol,
    kind: RuntimeCommandKind,
) -> PlaceTailCommand | AmendTailCommand:
    """Build a tail command for placement or amendment from runtime state."""
    request: PlaceOrderCommandRequest | AmendOrderCommandRequest
    if kind == RuntimeCommandKind.AMEND:
        request = tail_amend_request(state)
        order = tail_amend_order_dict(state)
    else:
        request = tail_place_request(state)
        order = tail_order_dict(state)
    if kind == RuntimeCommandKind.AMEND:
        return AmendTailCommand(
            kind=kind,
            symbol=symbol,
            request=cast(AmendOrderCommandRequest, request),
            pair_name=state.pair.name,
            legacy_order=order,
        )
    return PlaceTailCommand(
        kind=kind,
        symbol=symbol,
        request=cast(PlaceOrderCommandRequest, request),
        pair_name=state.pair.name,
        legacy_order=order,
    )


def with_played_quantity(state: PairCycleState, quantity: Decimal) -> PairCycleState:
    """Return updated pair state with a normalized played quantity."""
    return replace(state, played_quantity=max(quantity, Decimal("0")))


def tail_stop_price(state: PairCycleState) -> Decimal | float | None:
    """Resolve dynamic trail stop first, then the static transitional value."""
    if state.tail_trail is not None:
        return state.tail_trail.current_stop_price
    return state.pair.tail_price_spec
