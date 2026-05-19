from __future__ import annotations

from queue import Queue
from typing import Optional

import numpy as np

from kolabi.runtime.legacy.kola.chronos import Chronos
from kolabi.runtime.legacy.kola.utils.datefunc import now
from kolabi.runtime.multi_kola import LegacyMarketAuditeur
from kolabi.shared.config import ExchangeConfig, load_exchange_config
from kolabi.shared.core.bargain import Bargain


class MarketAuditeur(LegacyMarketAuditeur):
    """Adapter around the legacy MarketAuditeur using the new shared adapters."""

    def __init__(
        self,
        *,
        exchange: str = "binance",
        symbol: str = "BTCUSDT",
        config: Optional[ExchangeConfig] = None,
        **kwargs,
    ) -> None:
        super().__init__(symbol=symbol, platform=exchange, **kwargs)
        self.trading_plateform = exchange
        self._config = config

    def start_server(self) -> None:  # type: ignore[override]
        """Override to instantiate the new shared Bargain + Chronos."""
        self.fileDattente: Queue = Queue()
        self.fileDeConfirmation: Queue = Queue()
        if self.dbo is not None:
            self.brg = self.dbo
        else:
            config = self._config or load_exchange_config(
                self.trading_plateform, symbol=self.symbol
            )
            self._config = config
            self.brg = Bargain(self.trading_plateform, config)
        self.chrs = Chronos(
            self.brg,
            self.fileDattente,
            self.fileDeConfirmation,
            logger=self.logger,
        )
        self.chrs.start()
        try:
            self.resultats.loc[now(), :] = (self.balance(), np.nan)
        except ValueError:
            self.logger.warning("Unable to log initial balance for MarketAuditeur")
