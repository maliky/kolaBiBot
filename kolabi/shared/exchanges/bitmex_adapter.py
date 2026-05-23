"""BitMEX exchange adapter backed by the legacy client."""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from kolabi.shared.exchanges.bitmex_api.custom_api import BitMEX
from kolabi.shared.core.models import OrderAck, Position
from kolabi.shared.core.runtime_types import OrderQty, Price, StopPrice
from kolabi.shared.core.types import ExchangeABC


class BitmexAdapter(ExchangeABC):
    """Adapter that reuses the legacy BitMEX client."""

    _ORDER_TYPE_MAP = {
        "LIMIT": "Limit",
        "MARKET": "Market",
        "STOP": "Stop",
        "STOPLIMIT": "StopLimit",
        "MARKETIFTOUCHED": "MarketIfTouched",
        "LIMITIFTOUCHED": "LimitIfTouched",
        "MIT": "MarketIfTouched",
        "LIT": "LimitIfTouched",
    }

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        *,
        client_factory: Callable[..., Any] | None = None,
        **client_kwargs: Any,
    ) -> None:
        super().__init__(api_key, api_secret, base_url, symbol)
        factory = client_factory or BitMEX
        kwargs = dict(client_kwargs)
        kwargs.setdefault("orderIDPrefix", "mlk_")
        kwargs.setdefault("postOnly", False)
        kwargs.setdefault("timeout", 12)
        try:
            self.client = factory(
                base_url=base_url,
                symbol=symbol,
                apiKey=api_key,
                apiSecret=api_secret,
                **kwargs,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to initialise BitMEX client: {exc}") from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @classmethod
    def _normalize_type(cls, type_: str) -> str:
        key = type_.replace("_", "").upper()
        return cls._ORDER_TYPE_MAP.get(key, type_)

    @staticmethod
    def _first(payload: Any) -> Dict[str, Any]:
        if isinstance(payload, list) and payload:
            first = payload[0]
            return first if isinstance(first, dict) else {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _build_ack(data: Dict[str, Any]) -> OrderAck:
        return OrderAck(
            order_id=str(data.get("orderID", "")),
            status=data.get("ordStatus", ""),
            price=data.get("price"),
            orig_qty=data.get("orderQty"),
            executed_qty=data.get("cumQty"),
            side=data.get("side"),
        )

    # ------------------------------------------------------------------
    # ExchangeABC API
    # ------------------------------------------------------------------
    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Price | float | None = None,
        stopPx: StopPrice | float | None = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        opts: Dict[str, Any] = {"ordType": self._normalize_type(type_)}
        if price is not None:
            opts["price"] = float(price)
        if stopPx is not None:
            opts["stopPx"] = float(stopPx)
        opts.update(params)
        response = self.client.place(float(orderQty), side=side.lower(), asBulk=False, **opts)
        return self._build_ack(self._first(response))

    def amend_order(self, order_id: str, **params: Any) -> OrderAck:
        response = self.client.amend({"orderID": order_id}, **params)
        return self._build_ack(self._first(response))

    def cancel_order(self, order_id: str) -> OrderAck:
        response = self.client.cancel(order_id)
        data = self._first(response)
        if isinstance(data, dict):
            data.setdefault("ordStatus", data.get("ordStatus", "Canceled"))
        return self._build_ack(data)

    def get_position(self) -> Position:
        data = self.client.position(self.symbol) or {}
        qty = float(data.get("currentQty") or 0)
        entry = data.get("avgEntryPrice")
        entry_price = float(entry) if entry is not None else None
        return Position(symbol=self.symbol, qty=qty, entry_price=entry_price)

    def get_balance(self) -> float:
        margin = self.client.margin()
        return float(margin.get("availableMargin", 0))


# Alias expected by get_adapter
Adapter = BitmexAdapter
