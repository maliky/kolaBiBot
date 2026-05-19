from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Protocol


@dataclass
class TreeConfig:
    """Configuration for kolaBiTree ingestion jobs."""

    db_url: str
    symbol: str
    timeframe: str = "1m"
    source: str = "binance"


class PriceSource(Protocol):
    def stream(self, symbol: str, timeframe: str) -> Iterable[dict[str, Any]]:
        ...


class TreeService:
    """Coordinates ingestion and indicator computation."""

    def __init__(self, config: TreeConfig, source: PriceSource) -> None:
        self.config = config
        self.source = source

    def run_once(self) -> None:
        for candle in self.source.stream(self.config.symbol, self.config.timeframe):
            self.process_candle(candle)

    def process_candle(self, candle: dict[str, Any]) -> None:
        # TODO: write to DB + compute indicators
        raise NotImplementedError("TreeService.process_candle pending implementation")
