# -*- coding: utf-8 -*-
"""Kraken exchange connector placeholder."""
from __future__ import annotations

from typing import Any, Optional

from .base import BaseExchange


class KrakenExchange(BaseExchange):
    """Minimal placeholder for a Kraken connector."""

    def __init__(
        self,
        base_url: str = "https://api.kraken.com",
        rate_limit: float = 1.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url, rate_limit)
        self.client = None
        self.ws = None

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def cancel_order(self, order_id: Any) -> Any:
        raise NotImplementedError

    def get_balance(self, symbol: Optional[str] = None) -> Any:
        raise NotImplementedError

    def get_prices(self, symbol: Optional[str] = None) -> Any:
        raise NotImplementedError
