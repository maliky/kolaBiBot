from typing import Any, Dict

from kolaBot.kola.bitmex_api.custom_api import BitMEX
from kolaBot.kola.binance_api.client import Client as Binance


def _is_bitmex(client: Any) -> bool:
    return isinstance(client, BitMEX)


def _is_binance(client: Any) -> bool:
    return isinstance(client, Binance)


def place_order(client: Any, *args, **kwargs) -> Any:
    """Place an order using the underlying exchange client."""
    if _is_bitmex(client):
        return client.place(*args, **kwargs)
    if _is_binance(client):
        # Binance uses keyword arguments only
        return client.create_order(**kwargs)
    raise ValueError("Unsupported exchange client")


def cancel_order(client: Any, order_id: Any) -> Any:
    """Cancel an order."""
    if _is_bitmex(client):
        return client.cancel(order_id)
    if _is_binance(client):
        params = order_id if isinstance(order_id, dict) else {"orderId": order_id}
        return client.cancel_order(**params)
    raise ValueError("Unsupported exchange client")


def get_balance(client: Any, symbol: str | None = None) -> Any:
    """Return available balance for the exchange."""
    if _is_bitmex(client):
        data = client.margin()
        return data.get("availableMargin")
    if _is_binance(client):
        account = client.get_account()
        if symbol:
            base = symbol[:-4] if symbol.endswith("USDT") else symbol[:-3]
            for bal in account.get("balances", []):
                if bal.get("asset") == base:
                    return float(bal.get("free", 0))
        return account
    raise ValueError("Unsupported exchange client")


def get_prices(client: Any, symbol: str) -> Dict[str, Any]:
    """Return price information for a symbol."""
    if _is_bitmex(client):
        data = client.instrument(symbol)
        return {k: v for k, v in data.items() if "rice" in k}
    if _is_binance(client):
        ticker = client.get_orderbook_ticker(symbol=symbol)
        return {"bidPrice": float(ticker["bidPrice"]), "askPrice": float(ticker["askPrice"]) }
    raise ValueError("Unsupported exchange client")
