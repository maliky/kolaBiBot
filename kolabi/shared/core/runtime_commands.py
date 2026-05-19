from __future__ import annotations

from typing import Any

from kolabi.runtime.legacy.kola.orders.orders import (
    amend_prices,
    cancel_order,
    place,
    place_LIT,
    place_MIT,
    place_SL,
    place_at_market,
    place_stop,
)
from kolabi.shared.core.runtime_types import (
    OrderDict,
    OrderRole,
    RuntimeCommand,
    RuntimeCommandKind,
    Symbol,
    ValidationCondition,
)


def runtime_command_from_order(
    *,
    symbol: str,
    order: OrderDict,
    reason: str | None = None,
) -> RuntimeCommand:
    normalized = dict(order)
    ord_type = str(normalized.get("ordType", ""))
    if ord_type == "cancel":
        kind = RuntimeCommandKind.CANCEL
        command_reason = reason or OrderRole.CANCEL.value
    elif ord_type.startswith("amend"):
        kind = RuntimeCommandKind.AMEND
        command_reason = reason or OrderRole.AMEND.value
    else:
        kind = RuntimeCommandKind.PLACE
        command_reason = reason or OrderRole.PRIMARY.value
    return RuntimeCommand(
        kind=kind,
        symbol=Symbol(symbol),
        order=normalized,
        reason=command_reason,
    )


def timeout_override_minutes_for(command: RuntimeCommand) -> int | None:
    ord_type = command_order_type(command)
    if ord_type == "Market":
        return 5
    if ord_type == "cancel":
        return 1
    return None


def validation_conditions_for(
    command: RuntimeCommand,
    *,
    trailstop_sender: bool = False,
) -> tuple[ValidationCondition, ...]:
    ord_type = command_order_type(command)
    if ord_type.startswith("amend"):
        return ({"exectype": "Replaced", "orderstatus": "New"},)
    if ord_type == "cancel":
        return ({"exectype": "Canceled", "orderstatus": "Canceled"},)
    if ord_type in {"Stop", "MarketIfTouched", "StopLimit", "LimitIfTouched"} and trailstop_sender:
        return ({"exectype": "New", "orderstatus": "New"},)
    return ({"exectype": "Trade", "orderstatus": "Filled"},)


def execute_runtime_command(
    brg: object,
    command: RuntimeCommand,
    *,
    amend_absdelta: float,
) -> Any:
    order = dict(command.order or {})
    ord_type = command_order_type(command)
    order.pop("ordType", None)

    if command.kind == RuntimeCommandKind.CANCEL:
        cl_ord_id = str(order["clOrdID"])
        return cancel_order(brg, {"clOrdID": cl_ord_id})

    if command.kind == RuntimeCommandKind.AMEND:
        order_id = str(order["orderID"])
        new_price = float(order["newPrice"])
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
    order_qty = _quantity_from_order(order)

    if ord_type == "Market":
        return place_at_market(brg, order_qty, side, **order)
    if ord_type == "Limit":
        return place(brg, side, order_qty, float(order.pop("price")), **order)
    if ord_type == "Stop":
        return place_stop(brg, side, order_qty, float(order.pop("stopPx")), **order)
    if ord_type == "StopLimit":
        return place_SL(
            brg,
            side,
            order_qty,
            float(order.pop("stopPx")),
            float(order.pop("price")),
            **order,
        )
    if ord_type == "MarketIfTouched":
        return place_MIT(brg, side, order_qty, float(order.pop("stopPx")), **order)
    if ord_type == "LimitIfTouched":
        return place_LIT(
            brg,
            side,
            order_qty,
            float(order.pop("stopPx")),
            float(order.pop("price")),
            **order,
        )
    raise ValueError(f"Action type '{ord_type}' pas prise en compte")


def command_order_type(command: RuntimeCommand) -> str:
    return str((command.order or {}).get("ordType", ""))


def _quantity_from_order(order: OrderDict) -> Any:
    if "orderQty" in order:
        return order.pop("orderQty")
    if "quantity" in order:
        return order.pop("quantity")
    raise KeyError("orderQty")


__all__ = [
    "execute_runtime_command",
    "runtime_command_from_order",
    "timeout_override_minutes_for",
    "validation_conditions_for",
]
