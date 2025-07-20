"""Binance exchange adapter.

Maps our common order fields to Binance REST API fields:
- ``orderQty`` -> ``quantity``
- ``stopPx``   -> ``stopPrice``
- ``price``    -> ``price``

This adapter uses :class:`python_binance.client.Client` for REST calls and
implements basic rate limiting and precision handling using exchange filters.
Known quirks: only a subset of the API is supported and WebSockets are ignored.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

from binance.client import Client  # type: ignore[import-untyped]

from kola.core.types import ExchangeABC
from kola.core.models import OrderAck, Position


class BinanceAdapter(ExchangeABC):
    """Simple REST adapter around :mod:`python-binance`."""

    RATE_LIMIT = 1200  # weight per minute

    def __init__(self, api_key: str, api_secret: str, base_url: str, symbol: str) -> None:
        super().__init__(api_key, api_secret, base_url, symbol)
        self.client = Client(api_key, api_secret)
        self.client.API_URL = base_url
        self._window_start = time.time()
        self._query_weight = 0
        self.filters: Dict[str, float] = {}
        self._load_filters()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _load_filters(self) -> None:
        """Download symbol filters used for precision adjustments."""
        self._throttle()
        info = self.client.get_exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == self.symbol:
                for f in s.get("filters", []):
                    ft = f.get("filterType")
                    if ft == "LOT_SIZE":
                        self.filters["stepSize"] = float(f.get("stepSize", 0))
                        self.filters["minQty"] = float(f.get("minQty", 0))
                    elif ft == "PRICE_FILTER":
                        self.filters["tickSize"] = float(f.get("tickSize", 0))
                    elif ft == "MIN_NOTIONAL":
                        self.filters["minNotional"] = float(f.get("minNotional", 0))
                break

    def _throttle(self, weight: int = 1) -> None:
        """Very small query weight throttling."""
        now = time.time()
        if now - self._window_start > 60:
            self._window_start = now
            self._query_weight = 0
        if self._query_weight + weight > self.RATE_LIMIT:
            sleep_time = 60 - (now - self._window_start)
            if sleep_time > 0:
                time.sleep(sleep_time)
            self._window_start = time.time()
            self._query_weight = 0
        self._query_weight += weight

    # ------------------------------------------------------------------
    # Order utilities
    # ------------------------------------------------------------------
    def validate_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        step = self.filters.get("stepSize")
        tick = self.filters.get("tickSize")
        min_notional = self.filters.get("minNotional")
        if step and "quantity" in order:
            qty = float(order["quantity"])
            order["quantity"] = max(
                self.filters.get("minQty", 0),
                float(int(qty / step) * step),
            )
        if tick and "price" in order:
            price = float(order["price"])
            order["price"] = float(round(round(price / tick) * tick, 8))
        if min_notional and "quantity" in order and "price" in order:
            if float(order["quantity"]) * float(order["price"]) < min_notional:
                raise ValueError("Order notional below minimum")
        return order

    # ------------------------------------------------------------------
    # ExchangeABC API
    # ------------------------------------------------------------------
    def place_order(
        self,
        side: str,
        orderQty: float,
        price: Optional[float] = None,
        stopPx: Optional[float] = None,
        type_: str = "LIMIT",
    ) -> OrderAck:
        data: Dict[str, Any] = {
            "symbol": self.symbol,
            "side": side.upper(),
            "type": type_.upper(),
            "quantity": orderQty,
        }
        if price is not None:
            data["price"] = price
        if stopPx is not None:
            data["stopPrice"] = stopPx
        data = self.validate_order(data)
        self._throttle()
        resp = self.client.create_order(**data)
        return OrderAck(
            order_id=str(resp["orderId"]),
            status=resp.get("status", ""),
            price=float(resp.get("price", 0) or 0),
            orig_qty=float(resp.get("origQty", 0) or 0),
            executed_qty=float(resp.get("executedQty", 0) or 0),
            side=resp.get("side"),
        )

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        # Binance doesn't support in-place amend; emulate with cancel+new order
        self.cancel_order(order_id)
        new_params = {
            "side": params.get("side", "BUY"),
            "orderQty": params.get("orderQty"),
            "price": params.get("price"),
            "stopPx": params.get("stopPx"),
            "type_": params.get("type_", "LIMIT"),
        }
        return self.place_order(**new_params)

    def cancel_order(self, order_id: str) -> OrderAck:
        self._throttle()
        resp = self.client.cancel_order(symbol=self.symbol, orderId=order_id)
        return OrderAck(
            order_id=str(resp["orderId"]),
            status="CANCELED",
        )

    def get_position(self) -> Position:
        self._throttle()
        asset = self.symbol.replace("USDT", "")
        data = self.client.get_asset_balance(asset=asset)
        return Position(symbol=self.symbol, qty=float(data.get("free", 0)))

    def get_balance(self) -> float:
        self._throttle()
        data = self.client.get_asset_balance(asset="USDT")
        return float(data.get("free", 0))


# Alias expected by get_adapter
Adapter = BinanceAdapter
