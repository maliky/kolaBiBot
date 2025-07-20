# -*- coding: utf-8 -*-
"""Binance exchange connector wrapper."""
from __future__ import annotations

from typing import Any, Optional

from kolaBot.kola.binance_api.client import Client as BinanceClient
from kolaBot.kola.binance_api.websockets import BinanceSocketManager
from kolaBot.kola.settings import BINANCE_TEST_URL
from .base import BaseExchange


class BinanceExchange(BaseExchange):
    """Binance exchange implementation."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        symbol: str,
        base_url: str = BINANCE_TEST_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(base_url, rate_limit=0.0, symbol=symbol)
        self.client = BinanceClient(api_key=api_key, api_secret=api_secret, **kwargs)
        self.client.API_URL = base_url
        self.ws = BinanceSocketManager(self.client)

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        return self.client.create_order(**kwargs)

    def cancel_order(self, order_id: Any) -> Any:
        params = order_id if isinstance(order_id, dict) else {"orderId": order_id}
        return self.client.cancel_order(**params)

    def get_balance(self, symbol: Optional[str] = None) -> Any:
        account = self.client.get_account()
        if symbol:
            base = symbol[:-4] if symbol.endswith("USDT") else symbol[:-3]
            for bal in account.get("balances", []):
                if bal.get("asset") == base:
                    return float(bal.get("free", 0))
        return account

    def get_prices(self, symbol: Optional[str] = None) -> Any:
        ticker = self.client.get_orderbook_ticker(symbol=symbol or self.symbol)
        return {"bidPrice": float(ticker["bidPrice"]), "askPrice": float(ticker["askPrice"]) }
