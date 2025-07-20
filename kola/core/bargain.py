from __future__ import annotations

from typing import Optional

from kola.exchanges import get_adapter


class Bargain:
    """Minimal bargain object wiring exchange adapters."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        symbol: str,
        trading_platform: str = "binance",
    ) -> None:
        self.symbol = symbol
        Adapter = get_adapter(trading_platform)
        self.crypto_api = Adapter(
            api_key=api_key,
            api_secret=api_secret,
            base_url=base_url,
            symbol=symbol,
        )
