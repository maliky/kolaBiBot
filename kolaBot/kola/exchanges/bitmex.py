# -*- coding: utf-8 -*-
"""Wrapper around the original BitMEX connector."""
from __future__ import annotations

from typing import Any, Optional

from kolaBot.kola.bitmex_api.custom_api import BitMEX
from kolaBot.kola.settings import HTTP_SIMPLE_RATE_LIMITE, TEST_URL
from .base import BaseExchange


class BitmexExchange(BaseExchange):
    """BitMEX exchange implementation."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbol: str,
        base_url: str = TEST_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url, HTTP_SIMPLE_RATE_LIMITE, symbol)
        self.client = BitMEX(
            base_url=base_url,
            symbol=symbol,
            apiKey=api_key,
            apiSecret=api_secret,
            **kwargs,
        )
        self.ws = self.client.ws

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        return self.client.place(*args, **kwargs)

    def cancel_order(self, order_id: Any) -> Any:
        return self.client.cancel(order_id)

    def get_balance(self, symbol: Optional[str] = None) -> Any:
        data = self.client.margin()
        return data.get("availableMargin")

    def get_prices(self, symbol: Optional[str] = None) -> Any:
        data = self.client.instrument(symbol or self.symbol)
        return {k: v for k, v in data.items() if "rice" in k}
