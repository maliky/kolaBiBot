"""Ogun command interpreter for exchange side effects.

Purpose: execute typed bot commands against exchange-facing legacy order
functions while the runtime migrates toward pure reducers plus boundary shells.
Inputs: bot command values and exchange/bargain object.
Outputs: exchange acknowledgement payloads.
Side effects: network/exchange calls and order submissions/amend/cancel actions.
Important types: algebraic bot commands, `OrderDict`.
Role: interpreter shell.
Transitional: yes, legacy order helpers are isolated behind a lazy adapter.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Protocol, cast

from kolabi.shared.core.runtime_commands import command_order_type
from kolabi.shared.core.runtime_types import (
    AmendTailCommand,
    BotCommand,
    CancelCommand,
    OrderDict,
    PlaceHeadCommand,
    PlaceTailCommand,
    RuntimeCommandKind,
    decimal_to_float,
)


class LegacyOrderAdapterLike(Protocol):
    def place_at_market(self, brg: object, order_qty: float, side: str, **opts: object) -> Any: ...
    def place(self, brg: object, side: str, order_qty: float, price: float, **opts: object) -> Any: ...
    def place_stop(self, brg: object, side: str, order_qty: float, stop_px: float, **opts: object) -> Any: ...
    def place_sl(self, brg: object, side: str, order_qty: float, stop_px: float, price: float, **opts: object) -> Any: ...
    def place_mit(self, brg: object, side: str, order_qty: float, stop_px: float, **opts: object) -> Any: ...
    def place_lit(self, brg: object, side: str, order_qty: float, stop_px: float, price: float, **opts: object) -> Any: ...
    def amend_prices(
        self,
        brg: object,
        order_id: str,
        new_price: float,
        ord_type: str,
        side: str,
        *,
        absdelta: float,
        text: str,
    ) -> Any: ...
    def amend_order_qty(self, brg: object, order: dict[str, object], new_qty: float) -> Any: ...
    def cancel_order(self, brg: object, order: dict[str, object]) -> Any: ...


class LegacyOrderAdapter:
    """Lazy bridge to the legacy order helper module.

    This keeps the active bot path importable even when old helper dependencies
    are absent. Imports happen only at method call time.
    """

    def place_at_market(self, brg: object, order_qty: float, side: str, **opts: object) -> Any:
        from kolabi.runtime.kola.orders.orders import place_at_market

        return place_at_market(cast(Any, brg), order_qty, side, **opts)

    def place(self, brg: object, side: str, order_qty: float, price: float, **opts: object) -> Any:
        from kolabi.runtime.kola.orders.orders import place

        return place(cast(Any, brg), side, order_qty, price, **opts)

    def place_stop(self, brg: object, side: str, order_qty: float, stop_px: float, **opts: object) -> Any:
        from kolabi.runtime.kola.orders.orders import place_stop

        return place_stop(cast(Any, brg), side, order_qty, stop_px, **opts)

    def place_sl(self, brg: object, side: str, order_qty: float, stop_px: float, price: float, **opts: object) -> Any:
        from kolabi.runtime.kola.orders.orders import place_SL

        return place_SL(cast(Any, brg), side, order_qty, stop_px, price, **opts)

    def place_mit(self, brg: object, side: str, order_qty: float, stop_px: float, **opts: object) -> Any:
        from kolabi.runtime.kola.orders.orders import place_MIT

        return place_MIT(cast(Any, brg), side, order_qty, stop_px, **opts)

    def place_lit(self, brg: object, side: str, order_qty: float, stop_px: float, price: float, **opts: object) -> Any:
        from kolabi.runtime.kola.orders.orders import place_LIT

        return place_LIT(cast(Any, brg), side, order_qty, stop_px, price, **opts)

    def amend_prices(
        self,
        brg: object,
        order_id: str,
        new_price: float,
        ord_type: str,
        side: str,
        *,
        absdelta: float,
        text: str,
    ) -> Any:
        from kolabi.runtime.kola.orders.orders import amend_prices

        return amend_prices(
            cast(Any, brg),
            order_id,
            new_price,
            ord_type,
            side,
            absdelta=absdelta,
            text=text,
        )

    def amend_order_qty(self, brg: object, order: dict[str, object], new_qty: float) -> Any:
        from kolabi.runtime.kola.orders.orders import amend_orderQty

        return amend_orderQty(cast(Any, brg), order, new_qty)

    def cancel_order(self, brg: object, order: dict[str, object]) -> Any:
        from kolabi.runtime.kola.orders.orders import cancel_order

        return cancel_order(cast(Any, brg), order)


def execute_runtime_command(
    brg: object,
    command: BotCommand,
    *,
    amend_absdelta: float,
    adapter: LegacyOrderAdapterLike | None = None,
) -> Any:
    if command.kind not in {
        RuntimeCommandKind.PLACE,
        RuntimeCommandKind.AMEND,
        RuntimeCommandKind.CANCEL,
    }:
        raise ValueError(f"Unsupported runtime command kind: {command.kind!r}")
    adapter = adapter or LegacyOrderAdapter()
    order = _legacy_order_from_command(command)
    ord_type = command_order_type(command)
    order.pop("ordType", None)

    if isinstance(command, CancelCommand):
        cl_ord_id = str(order["clOrdID"])
        return adapter.cancel_order(brg, {"clOrdID": cl_ord_id})

    if isinstance(command, AmendTailCommand):
        order_id = str(order["orderID"])
        side = str(order["side"])
        text = str(order.get("text", ""))
        new_price = _optional_float_price(order.get("newPrice"))
        new_qty = _optional_float_quantity(order.get("newQty"))
        if new_price is None and new_qty is None:
            raise ValueError("AMEND requires at least one planned change")
        if new_price is not None and new_qty is not None:
            return _amend_price_and_quantity(
                brg,
                order_id,
                new_price,
                new_qty,
                ord_type,
                side,
                amend_absdelta=amend_absdelta,
                text=text,
            )
        if new_price is not None:
            return adapter.amend_prices(
                brg,
                order_id,
                new_price,
                ord_type,
                side,
                absdelta=amend_absdelta,
                text=text,
            )
        return adapter.amend_order_qty(
            brg,
            {"orderID": order_id, "orderQty": new_qty},
            cast(float, new_qty),
        )

    side = str(order.pop("side"))
    order_qty = _as_float_quantity(_quantity_from_order(order))
    opts = cast(dict[str, Any], order)

    if ord_type == "Market":
        return adapter.place_at_market(brg, order_qty, side, **opts)
    if ord_type == "Limit":
        price = _as_float_price(order.pop("price"))
        return adapter.place(brg, side, order_qty, price, **opts)
    if ord_type == "Stop":
        stop_px = _as_float_price(order.pop("stopPx"))
        return adapter.place_stop(brg, side, order_qty, stop_px, **opts)
    if ord_type == "StopLimit":
        stop_px = _as_float_price(order.pop("stopPx"))
        price = _as_float_price(order.pop("price"))
        return adapter.place_sl(brg, side, order_qty, stop_px, price, **opts)
    if ord_type == "MarketIfTouched":
        stop_px = _as_float_price(order.pop("stopPx"))
        return adapter.place_mit(brg, side, order_qty, stop_px, **opts)
    if ord_type == "LimitIfTouched":
        stop_px = _as_float_price(order.pop("stopPx"))
        price = _as_float_price(order.pop("price"))
        return adapter.place_lit(brg, side, order_qty, stop_px, price, **opts)
    raise ValueError(f"Action type '{ord_type}' pas prise en compte")


def _legacy_order_from_command(command: BotCommand) -> OrderDict:
    request = command.request
    if isinstance(command, CancelCommand):
        return {
            "pair_name": request.pair_name,
            "ordType": request.ordType,
            "clOrdID": request.clOrdID,
        }
    if isinstance(command, AmendTailCommand):
        order: OrderDict = {
            "pair_name": request.pair_name,
            "ordType": request.ordType,
            "side": request.side,
            "orderID": request.orderID,
        }
        if request.clOrdID is not None:
            order["clOrdID"] = request.clOrdID
        if request.newPrice is not None:
            order["newPrice"] = request.newPrice
        if request.newQty is not None:
            order["newQty"] = request.newQty
        if request.text is not None:
            order["text"] = request.text
        return order
    order: OrderDict = {
        "pair_name": request.pair_name,
        "ordType": request.ordType,
        "side": request.side,
    }
    if request.orderQty is not None:
        order["orderQty"] = request.orderQty
    if request.price is not None:
        order["price"] = request.price
    if request.stopPx is not None:
        order["stopPx"] = request.stopPx
    if request.execInst is not None:
        order["execInst"] = request.execInst
    if request.clOrdID is not None:
        order["clOrdID"] = request.clOrdID
    if request.text is not None:
        order["text"] = request.text
    if request.oDelta is not None:
        order["oDelta"] = request.oDelta
    return order


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


def _optional_float_price(value: object | None) -> float | None:
    if value is None:
        return None
    return _as_float_price(value)


def _optional_float_quantity(value: object | None) -> float | None:
    if value is None:
        return None
    return _as_float_quantity(value)


def _amend_price_and_quantity(
    brg: object,
    order_id: str,
    new_price: float,
    new_qty: float,
    ord_type: str,
    side: str,
    *,
    amend_absdelta: float,
    text: str,
) -> Any:
    order_update: dict[str, object] = {"orderID": order_id, "orderQty": new_qty}
    if ord_type in {"Stop", "MarketIfTouched"}:
        order_update["stopPx"] = new_price
    elif ord_type == "Limit":
        order_update["price"] = new_price
    elif ord_type in {"StopLimit", "LimitIfTouched"}:
        order_update["price"] = new_price
        order_update["stopPx"] = new_price + amend_absdelta if side == "buy" else new_price - amend_absdelta
    else:
        raise ValueError(f"Action type '{ord_type}' pas prise en compte")
    if text:
        order_update["text"] = text
    return cast(Any, brg).crypto_api.amend({"orderID": order_id}, **order_update)
