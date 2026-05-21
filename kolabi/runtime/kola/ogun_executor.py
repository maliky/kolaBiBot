"""Ogun command interpreter for exchange side effects.

Purpose: execute typed runtime commands against exchange-facing legacy order
functions while the runtime migrates toward pure reducers plus boundary shells.
Inputs: `RuntimeCommand` values and exchange/bargain object.
Outputs: exchange acknowledgement payloads.
Side effects: network/exchange calls and order submissions/amend/cancel actions.
Important types: `RuntimeCommand`, `RuntimeCommandKind`, `OrderDict`.
Role: interpreter shell.
Transitional: yes, still calls legacy order helpers.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from kolabi.runtime.kola.orders.orders import (
    amend_prices,
    cancel_order,
    place,
    place_at_market,
    place_LIT,
    place_MIT,
    place_SL,
    place_stop,
)
from kolabi.shared.core.runtime_commands import command_order_type
from kolabi.shared.core.runtime_types import OrderDict, RuntimeCommand, RuntimeCommandKind, decimal_to_float


def execute_runtime_command(
    brg: object,
    command: RuntimeCommand,
    *,
    amend_absdelta: float,
) -> Any:
    order = cast(OrderDict, dict(command.order or {}))
    if command.kind not in {
        RuntimeCommandKind.PLACE,
        RuntimeCommandKind.AMEND,
        RuntimeCommandKind.CANCEL,
    }:
        raise ValueError(f"Unsupported runtime command kind: {command.kind!r}")

    ord_type = command_order_type(command)
    order.pop("ordType", None)

    if command.kind == RuntimeCommandKind.CANCEL:
        cl_ord_id = str(order["clOrdID"])
        return cancel_order(cast(Any, brg), {"clOrdID": cl_ord_id})

    if command.kind == RuntimeCommandKind.AMEND:
        order_id = str(order["orderID"])
        new_price = _as_float_price(order["newPrice"])
        side = str(order["side"])
        text = str(order.get("text", ""))
        return amend_prices(
            brg,
            order_id,
            new_price,
            ord_type,
            side,
            absdelta=amend_absdelta,
            text=text,
        )

    side = str(order.pop("side"))
    order_qty = _as_float_quantity(_quantity_from_order(order))
    opts = cast(dict[str, Any], order)

    if ord_type == "Market":
        return place_at_market(cast(Any, brg), order_qty, side, **opts)
    if ord_type == "Limit":
        price = _as_float_price(order.pop("price"))
        return place(cast(Any, brg), side, order_qty, price, **opts)
    if ord_type == "Stop":
        stop_px = _as_float_price(order.pop("stopPx"))
        return place_stop(cast(Any, brg), side, order_qty, stop_px, **opts)
    if ord_type == "StopLimit":
        stop_px = _as_float_price(order.pop("stopPx"))
        price = _as_float_price(order.pop("price"))
        return place_SL(cast(Any, brg), side, order_qty, stop_px, price, **opts)
    if ord_type == "MarketIfTouched":
        stop_px = _as_float_price(order.pop("stopPx"))
        return place_MIT(cast(Any, brg), side, order_qty, stop_px, **opts)
    if ord_type == "LimitIfTouched":
        stop_px = _as_float_price(order.pop("stopPx"))
        price = _as_float_price(order.pop("price"))
        return place_LIT(cast(Any, brg), side, order_qty, stop_px, price, **opts)
    raise ValueError(f"Action type '{ord_type}' pas prise en compte")


def _quantity_from_order(order: OrderDict) -> Any:
    if "orderQty" in order:
        return order.pop("orderQty")
    if "quantity" in order:
        return order.pop("quantity")
    raise KeyError("orderQty")


def _as_float_price(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, str)):
        return float(Decimal(str(value)))
    raise TypeError(f"Unsupported price value type: {type(value)!r}")


def _as_float_quantity(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, str)):
        return decimal_to_float(value)
    raise TypeError(f"Unsupported quantity value type: {type(value)!r}")
