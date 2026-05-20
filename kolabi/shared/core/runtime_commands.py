"""Runtime command translation and dispatch boundary.

Purpose: translate legacy order dict payloads into typed runtime commands and
derive pure validation/timeout rules.
Inputs: `OrderDict` payloads and `RuntimeCommand` instances.
Outputs: normalized commands and role payloads.
Side effects: none.
Important types: `RuntimeCommand`, `RuntimeCommandKind`, `OrderDict`,
`HeadCommandPayload`, `TailCommandPayload`.
Role: boundary adapter.
Transitional: yes, still bridges legacy order function signatures.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

from kolabi.shared.core.runtime_types import (
    HeadCommandPayload,
    OrderDict,
    OrderRole,
    RuntimeCommand,
    RuntimeCommandKind,
    Symbol,
    TailCommandPayload,
    ValidationCondition,
)


def runtime_command_from_order(
    *,
    symbol: str,
    order: OrderDict,
    reason: str | None = None,
) -> RuntimeCommand:
    normalized = cast(OrderDict, dict(order))
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


def command_order_type(command: RuntimeCommand) -> str:
    return str((command.order or {}).get("ordType", ""))


def command_payload_for_role(
    command: RuntimeCommand,
    *,
    role: OrderRole,
) -> HeadCommandPayload | TailCommandPayload:
    order = cast(OrderDict, dict(command.order or {}))
    if command.kind == RuntimeCommandKind.CANCEL:
        request: dict[str, object] = {
            "ordType": "cancel",
            "clOrdID": str(order["clOrdID"]),
        }
    elif command.kind == RuntimeCommandKind.AMEND:
        amend_request: dict[str, object] = {
            "ordType": str(order["ordType"]),
            "side": str(order["side"]),
            "orderID": str(order["orderID"]),
            "newPrice": _as_float_price(order["newPrice"]),
            "text": str(order.get("text", "")),
        }
        request = amend_request
    else:
        request = _new_order_request_from(command)
    payload = {
        "role": role.value,
        "command": command.kind,
        "request": request,
    }
    if role == OrderRole.TAIL:
        return cast(TailCommandPayload, payload)
    return cast(HeadCommandPayload, payload)


def _new_order_request_from(command: RuntimeCommand) -> dict[str, object]:
    order = cast(OrderDict, dict(command.order or {}))
    request: dict[str, object] = {
        "ordType": str(order["ordType"]),
        "side": str(order["side"]),
    }
    if "orderQty" in order:
        request["orderQty"] = order["orderQty"]
    if "quantity" in order:
        request["quantity"] = order["quantity"]
    for key in ("price", "stopPx", "execInst", "clOrdID", "text", "oDelta"):
        if key in order:
            request[key] = order[key]
    return request


def _as_float_price(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float, str)):
        return float(Decimal(str(value)))
    raise TypeError(f"Unsupported price value type: {type(value)!r}")


__all__ = [
    "command_payload_for_role",
    "runtime_command_from_order",
    "timeout_override_minutes_for",
    "validation_conditions_for",
]
