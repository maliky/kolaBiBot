from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class KrakenFuturesOrderType(StrEnum):
    """Canonical Kraken Futures wire values for orderType."""

    LIMIT = "lmt"
    POST_ONLY_LIMIT = "post"
    MARKET = "mkt"
    IMMEDIATE_OR_CANCEL = "ioc"
    STOP = "stp"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TRAILING_STOP_LIMIT = "trailing_stop_limit"


class KrakenFuturesStandardOrder(StrEnum):
    """Canonical standard-order intents used inside the repo.

    These are not Kraken wire values. They describe the trading intent in a
    stable vocabulary, then map to Kraken Futures request fields.
    """

    LIMIT = "limit"
    MARKET = "market"
    STOP_LOSS_MARKET = "stop_loss_market"
    STOP_LOSS_LIMIT = "stop_loss_limit"
    TAKE_PROFIT_MARKET = "take_profit_market"
    TAKE_PROFIT_LIMIT = "take_profit_limit"
    TRAILING_STOP_MARKET = "trailing_stop_market"
    TRAILING_STOP_LIMIT = "trailing_stop_limit"


class KrakenTrailingDeviationUnit(StrEnum):
    """Units accepted by Kraken Futures for native trailing stops."""

    PERCENT = "PERCENT"
    QUOTE_CURRENCY = "QUOTE_CURRENCY"


@dataclass(frozen=True)
class SendOrderContract:
    """Ordered Kraken Futures sendorder payload.

    The Futures REST API is sensitive to field naming and argument order. This
    dataclass centralises the serializer so adapters do not improvise wire
    values field-by-field.
    """

    order_type: KrakenFuturesOrderType
    symbol: str
    side: str
    size: float
    limit_price: float | None = None
    stop_price: float | None = None
    cli_ord_id: str | None = None
    reduce_only: bool = False
    post_only: bool = False
    trigger_signal: str | None = None
    trailing_stop_max_deviation: float | None = None
    trailing_stop_deviation_unit: KrakenTrailingDeviationUnit | None = None
    tag: str | None = None

    def as_params(self) -> list[tuple[str, Any]]:
        """Return ordered form fields for `/sendorder`."""
        return [
            ("orderType", self.order_type.value),
            ("symbol", self.symbol),
            ("side", self.side),
            ("size", self.size),
            ("limitPrice", self.limit_price),
            ("stopPrice", self.stop_price),
            ("cliOrdId", self.cli_ord_id),
            ("reduceOnly", self.reduce_only),
            ("triggerSignal", self.trigger_signal),
            ("trailingStopMaxDeviation", self.trailing_stop_max_deviation),
            (
                "trailingStopDeviationUnit",
                (
                    self.trailing_stop_deviation_unit.value
                    if self.trailing_stop_deviation_unit is not None
                    else None
                ),
            ),
            ("tag", self.tag),
        ]


def build_send_order_contract(
    *,
    ord_type: str,
    symbol: str,
    side: str,
    size: float,
    price: float | None,
    stop_price: float | None,
    fallback_market_price: float | None,
    cli_ord_id: str | None,
    reduce_only: bool,
    post_only: bool,
    trigger_signal: str | None,
    trailing_stop_max_deviation: float | None,
    trailing_stop_deviation_unit: str | None,
    tag: str | None,
) -> SendOrderContract:
    """Map legacy order inputs to a typed Kraken Futures sendorder contract."""
    order_type, limit_price, resolved_stop_price = map_standard_order_to_wire(
        ord_type=ord_type,
        price=price,
        stop_price=stop_price,
        fallback_market_price=fallback_market_price,
        post_only=post_only,
    )
    return SendOrderContract(
        order_type=order_type,
        symbol=symbol,
        side=side.lower(),
        size=size,
        limit_price=limit_price,
        stop_price=resolved_stop_price,
        cli_ord_id=cli_ord_id,
        reduce_only=reduce_only,
        post_only=post_only,
        trigger_signal=trigger_signal,
        trailing_stop_max_deviation=trailing_stop_max_deviation,
        trailing_stop_deviation_unit=normalize_trailing_deviation_unit(
            trailing_stop_deviation_unit
        ),
        tag=tag[:64] if tag else None,
    )


def normalize_standard_order(ord_type: str) -> KrakenFuturesStandardOrder:
    """Normalize repo aliases and legacy names to one standard-order intent."""
    normalized = ord_type.replace("_", "").replace("-", "").strip().lower()
    aliases = {
        "l": KrakenFuturesStandardOrder.LIMIT,
        "limit": KrakenFuturesStandardOrder.LIMIT,
        "m": KrakenFuturesStandardOrder.MARKET,
        "market": KrakenFuturesStandardOrder.MARKET,
        "s": KrakenFuturesStandardOrder.STOP_LOSS_MARKET,
        "stop": KrakenFuturesStandardOrder.STOP_LOSS_MARKET,
        "stoploss": KrakenFuturesStandardOrder.STOP_LOSS_MARKET,
        "stoplossmarket": KrakenFuturesStandardOrder.STOP_LOSS_MARKET,
        "triggerentry": KrakenFuturesStandardOrder.STOP_LOSS_MARKET,
        "triggerentrymarket": KrakenFuturesStandardOrder.STOP_LOSS_MARKET,
        "sl": KrakenFuturesStandardOrder.STOP_LOSS_LIMIT,
        "stoplimit": KrakenFuturesStandardOrder.STOP_LOSS_LIMIT,
        "stoplosslimit": KrakenFuturesStandardOrder.STOP_LOSS_LIMIT,
        "triggerentrylimit": KrakenFuturesStandardOrder.STOP_LOSS_LIMIT,
        "mt": KrakenFuturesStandardOrder.TAKE_PROFIT_MARKET,
        "marketiftouched": KrakenFuturesStandardOrder.TAKE_PROFIT_MARKET,
        "takeprofit": KrakenFuturesStandardOrder.TAKE_PROFIT_MARKET,
        "takeprofitmarket": KrakenFuturesStandardOrder.TAKE_PROFIT_MARKET,
        "lt": KrakenFuturesStandardOrder.TAKE_PROFIT_LIMIT,
        "limitiftouched": KrakenFuturesStandardOrder.TAKE_PROFIT_LIMIT,
        "takeprofitlimit": KrakenFuturesStandardOrder.TAKE_PROFIT_LIMIT,
        "trailingstop": KrakenFuturesStandardOrder.TRAILING_STOP_MARKET,
        "trailingstopmarket": KrakenFuturesStandardOrder.TRAILING_STOP_MARKET,
        "trailingstoplimit": KrakenFuturesStandardOrder.TRAILING_STOP_LIMIT,
    }
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported Kraken Futures order type '{ord_type}'") from exc


def map_standard_order_to_wire(
    *,
    ord_type: str,
    price: float | None,
    stop_price: float | None,
    fallback_market_price: float | None,
    post_only: bool = False,
) -> tuple[KrakenFuturesOrderType, float | None, float | None]:
    """Map standard-order intents to Kraken Futures wire values."""
    standard_order = normalize_standard_order(ord_type)
    if standard_order == KrakenFuturesStandardOrder.MARKET:
        _reject_unsupported_post_only(standard_order, post_only)
        del price
        del fallback_market_price
        return KrakenFuturesOrderType.MARKET, None, None
    if standard_order == KrakenFuturesStandardOrder.LIMIT:
        order_type = (
            KrakenFuturesOrderType.POST_ONLY_LIMIT
            if post_only
            else KrakenFuturesOrderType.LIMIT
        )
        return order_type, price, None
    if standard_order == KrakenFuturesStandardOrder.STOP_LOSS_MARKET:
        _reject_unsupported_post_only(standard_order, post_only)
        return KrakenFuturesOrderType.STOP, None, stop_price
    if standard_order == KrakenFuturesStandardOrder.STOP_LOSS_LIMIT:
        _reject_unsupported_post_only(standard_order, post_only)
        return KrakenFuturesOrderType.STOP, price, stop_price
    if standard_order == KrakenFuturesStandardOrder.TAKE_PROFIT_MARKET:
        _reject_unsupported_post_only(standard_order, post_only)
        return KrakenFuturesOrderType.TAKE_PROFIT, None, stop_price
    if standard_order == KrakenFuturesStandardOrder.TAKE_PROFIT_LIMIT:
        _reject_unsupported_post_only(standard_order, post_only)
        return KrakenFuturesOrderType.TAKE_PROFIT, price, stop_price
    if standard_order == KrakenFuturesStandardOrder.TRAILING_STOP_MARKET:
        _reject_unsupported_post_only(standard_order, post_only)
        return KrakenFuturesOrderType.TRAILING_STOP, None, None
    if standard_order == KrakenFuturesStandardOrder.TRAILING_STOP_LIMIT:
        _reject_unsupported_post_only(standard_order, post_only)
        return KrakenFuturesOrderType.TRAILING_STOP_LIMIT, price, None
    raise ValueError(f"Unsupported Kraken Futures order type '{ord_type}'")


def _reject_unsupported_post_only(
    standard_order: KrakenFuturesStandardOrder,
    post_only: bool,
) -> None:
    if post_only:
        raise ValueError(
            "Kraken Futures post-only is only supported for limit orders; "
            f"got {standard_order.value}."
        )


def normalize_trailing_deviation_unit(
    unit: str | None,
) -> KrakenTrailingDeviationUnit | None:
    """Normalize user-provided trailing-stop unit values."""
    if unit in (None, ""):
        return None
    normalized = str(unit).replace("-", "_").strip().upper()
    if normalized == "PERCENT":
        return KrakenTrailingDeviationUnit.PERCENT
    if normalized == "QUOTE_CURRENCY":
        return KrakenTrailingDeviationUnit.QUOTE_CURRENCY
    raise ValueError(f"Unsupported trailing stop deviation unit '{unit}'")
