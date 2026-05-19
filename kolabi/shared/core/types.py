from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from .models import OrderAck, Position
from .runtime_types import OrderID, OrderQty, Price, StopPrice


class ExchangeABC(ABC):
    """Abstract base class for exchange adapters."""

    def __init__(self, api_key: str, api_secret: str, base_url: str, symbol: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.symbol = symbol

    @abstractmethod
    def place_order(
        self,
        side: str,
        orderQty: OrderQty | float,
        price: Optional[Price | float] = None,
        stopPx: Optional[StopPrice | float] = None,
        type_: str = "LIMIT",
        **params: Any,
    ) -> OrderAck:
        """Place a new order and return an acknowledgement."""

    @abstractmethod
    def amend_order(self, order_id: OrderID | str, **params: float) -> OrderAck:
        """Modify an existing order and return an acknowledgement."""

    @abstractmethod
    def cancel_order(self, order_id: OrderID | str) -> OrderAck:
        """Cancel an order and return an acknowledgement."""

    @abstractmethod
    def get_position(self) -> Position:
        """Return the current position for the configured symbol."""

    @abstractmethod
    def get_balance(self) -> float:
        """Return account balance."""
