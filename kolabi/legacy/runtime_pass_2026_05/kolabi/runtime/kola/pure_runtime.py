"""Pure runtime decision helpers.

Purpose: hold side-effect-free decision logic extracted from legacy runtime
objects (condition truth checks, hooked-order update math, payload derivation).
Inputs: scalar values and typed payload fragments.
Outputs: booleans, typed update records, and `OrderDict` command payloads.
Side effects: none.
Important types: `OrderDict`, `Quantity`, `ComparisonOp`, `HookedOrderUpdate`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, cast

from kolabi.runtime.kola.utils.pricefunc import get_prix_decl, setdef_stopPrice
from kolabi.shared.core.runtime_types import (
    OrderDict,
    OrderQty,
    Price,
    Quantity,
    StopPrice,
    decimal_to_float,
    to_decimal,
)

ComparisonOp = Literal["<", ">", "==", "!="]
SideLiteral = Literal["buy", "sell"]
OrderTypeLiteral = Literal["Limit", "Stop", "MarketIfTouched", "StopLimit", "LimitIfTouched"]


def evaluate_comparison(left: Any, op: ComparisonOp, right: Any) -> bool:
    if op == "<":
        return bool(left < right)
    if op == ">":
        return bool(left > right)
    if op == "==":
        return bool(left == right)
    return bool(left != right)


def condition_truth_value(
    *,
    genre: str,
    op: ComparisonOp,
    value: Any,
    current_price: float | None,
    current_time: datetime,
    hook_matched: bool | None,
) -> bool:
    if genre == "temps":
        return evaluate_comparison(current_time, op, value)
    if genre == "hook":
        return bool(hook_matched)
    if current_price is None:
        raise ValueError(f"Missing current_price for genre={genre}")
    return evaluate_comparison(current_price, op, value)


@dataclass(frozen=True)
class HookedOrderUpdate:
    price: Decimal | None
    stop_px: Decimal | None


def derive_hooked_order_update(
    *,
    side: str,
    old_price: Decimal | float | int | str | None,
    old_stop_px: Decimal | float | int | str | None,
    condition_high_price: float,
    condition_low_price: float,
) -> HookedOrderUpdate:
    decl_price = to_decimal({"buy": condition_low_price, "sell": condition_high_price}[side])
    if old_price is None:
        next_stop = None if old_stop_px is None else to_decimal(old_stop_px)
    elif old_stop_px is None:
        next_stop = None
    else:
        price_delta = to_decimal(old_price) - to_decimal(old_stop_px)
        next_stop = decl_price - price_delta
    return HookedOrderUpdate(
        price=decl_price if old_price is not None else None,
        stop_px=next_stop,
    )


def normalize_amend_order_type(ordertype: str) -> str:
    return ordertype if ordertype.startswith("amend") else f"amend{ordertype}"


def build_order_payload(
    *,
    side: str,
    quantity: Quantity,
    op_type: str,
    ord_type: str,
    exec_inst: str,
    prices: tuple[float, float] | None,
    absdelta: float,
    text: str | None,
) -> OrderDict:
    if prices is None and ord_type != "Market":
        raise ValueError(f"prices are required for ord_type={ord_type}")
    order: OrderDict = {
        "side": side,
        "orderQty": cast(OrderQty, to_decimal(int(quantity))),
        "ordType": ord_type,
        "execInst": exec_inst,
        "text": text,
    }
    typed_side = cast(SideLiteral, side)
    typed_ord_type = cast(OrderTypeLiteral, ord_type)
    if ord_type == "Limit":
        order["price"] = cast(
            Price,
            Decimal(
                str(
                    get_prix_decl(
                        cast(tuple[float, float], prices),
                        typed_side,
                        typed_ord_type,
                    )
                )
            ),
        )
    elif ord_type in ["Stop", "MarketIfTouched"]:
        order["stopPx"] = cast(
            StopPrice,
            Decimal(
                str(
                    get_prix_decl(
                        cast(tuple[float, float], prices),
                        typed_side,
                        typed_ord_type,
                    )
                )
            ),
        )
    elif ord_type in ["StopLimit", "LimitIfTouched"]:
        price = Decimal(
            str(get_prix_decl(cast(tuple[float, float], prices), typed_side, typed_ord_type))
        )
        order["price"] = cast(Price, price)
        order["stopPx"] = cast(
            StopPrice,
            Decimal(
            str(
                setdef_stopPrice(
                    decimal_to_float(price),
                    typed_side,
                    typed_ord_type,
                    absdelta,
                )
            )
            ),
        )

    _ = op_type
    return order


def ref_tail_from_reference(*, ref_price: float, head: str, tail_percent: float) -> Decimal:
    direction = Decimal("1") if head.lower() == "buy" else Decimal("-1")
    ref_price_dec = to_decimal(ref_price)
    tail_percent_dec = to_decimal(tail_percent)
    return ref_price_dec + (
        -direction * (ref_price_dec * tail_percent_dec / Decimal("100"))
    )
