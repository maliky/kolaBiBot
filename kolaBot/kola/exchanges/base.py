# -*- coding: utf-8 -*-
"""Base class for exchange connectors."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseExchange(ABC):
    """Abstract exchange connector interface."""

    def __init__(self, base_url: str, rate_limit: float, symbol: Optional[str] = None) -> None:
        self.base_url = base_url
        self.rate_limit = rate_limit
        self.symbol = symbol
        self.client: Any = None
        self.ws: Any = None

    @abstractmethod
    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        """Place an order."""
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: Any) -> Any:
        """Cancel an order."""
        raise NotImplementedError

    @abstractmethod
    def get_balance(self, symbol: Optional[str] = None) -> Any:
        """Return balance data."""
        raise NotImplementedError

    @abstractmethod
    def get_prices(self, symbol: Optional[str] = None) -> Any:
        """Return prices for a symbol."""
        raise NotImplementedError

    def setup_websocket(self) -> None:
        """Optional websocket initialisation."""
        pass
