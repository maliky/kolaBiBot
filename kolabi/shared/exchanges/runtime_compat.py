"""Legacy runtime exchange helpers shared across exchange adapters.

This module becomes the source of truth for the thin dispatch helpers that the
legacy runtime expects. The historical ``kolabi.runtime.legacy.kola.exchange``
module now re-exports these names for compatibility.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from kolabi.runtime.legacy.kola.binance_api.client import Client as Binance
from kolabi.runtime.legacy.kola.bitmex_api.custom_api import BitMEX
from kolabi.shared.exchanges.kraken_adapter import KrakenFuturesAdapter


class _SupportsPlace(Protocol):
    def place(self, *args: object, **kwargs: object) -> Any: ...


class _SupportsCancel(Protocol):
    def cancel(self, order_id: object) -> Any: ...


class _SupportsMargin(Protocol):
    def margin(self) -> dict[str, object]: ...


class _SupportsInstrument(Protocol):
    def instrument(self, symbol: str) -> dict[str, object]: ...


class _SupportsBinanceCreateOrder(Protocol):
    def create_order(self, **kwargs: object) -> Any: ...


class _SupportsBinanceCancelOrder(Protocol):
    def cancel_order(self, **kwargs: object) -> Any: ...


class _SupportsBinanceAccount(Protocol):
    def get_account(self) -> dict[str, object]: ...


class _SupportsBinanceTicker(Protocol):
    def get_orderbook_ticker(self, *, symbol: str) -> dict[str, object]: ...


def _is_bitmex(client: Any) -> bool:
    return isinstance(client, BitMEX)


def _is_binance(client: Any) -> bool:
    return isinstance(client, Binance)


def _is_kraken(client: Any) -> bool:
    return isinstance(client, KrakenFuturesAdapter)


def place_order(client: Any, *args: object, **kwargs: object) -> Any:
    """Place an order using the underlying exchange client."""
    if _is_bitmex(client):
        return cast(_SupportsPlace, client).place(*args, **kwargs)
    if _is_binance(client):
        return cast(_SupportsBinanceCreateOrder, client).create_order(**kwargs)
    if _is_kraken(client):
        return cast(_SupportsPlace, client).place(*args, **kwargs)
    raise ValueError("Unsupported exchange client")


def cancel_order(client: Any, order_id: Any) -> Any:
    """Cancel an order."""
    if _is_bitmex(client):
        return cast(_SupportsCancel, client).cancel(order_id)
    if _is_binance(client):
        params = order_id if isinstance(order_id, dict) else {"orderId": order_id}
        return cast(_SupportsBinanceCancelOrder, client).cancel_order(**params)
    if _is_kraken(client):
        target = order_id if isinstance(order_id, str) else order_id.get("orderID", order_id)
        return cast(_SupportsCancel, client).cancel(target)
    raise ValueError("Unsupported exchange client")


def get_balance(client: Any, symbol: str | None = None) -> Any:
    """Return available balance for the exchange."""
    if _is_bitmex(client):
        data = cast(_SupportsMargin, client).margin()
        return data.get("availableMargin")
    if _is_binance(client):
        account = cast(_SupportsBinanceAccount, client).get_account()
        if symbol:
            base = symbol[:-4] if symbol.endswith("USDT") else symbol[:-3]
            for balance in account.get("balances", []):
                if isinstance(balance, dict) and balance.get("asset") == base:
                    return float(balance.get("free", 0))
        return account
    if _is_kraken(client):
        return cast(_SupportsMargin, client).margin().get("availableMargin")
    raise ValueError("Unsupported exchange client")


def get_prices(client: Any, symbol: str) -> dict[str, Any]:
    """Return price information for a symbol."""
    if _is_bitmex(client):
        data = cast(_SupportsInstrument, client).instrument(symbol)
        return {key: value for key, value in data.items() if "rice" in key}
    if _is_binance(client):
        ticker = cast(_SupportsBinanceTicker, client).get_orderbook_ticker(symbol=symbol)
        return {
            "bidPrice": float(ticker["bidPrice"]),
            "askPrice": float(ticker["askPrice"]),
        }
    if _is_kraken(client):
        return cast(dict[str, Any], cast(_SupportsInstrument, client).instrument(symbol))
    raise ValueError("Unsupported exchange client")


__all__ = [
    "cancel_order",
    "get_balance",
    "get_prices",
    "place_order",
]
