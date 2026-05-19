from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from kolabi.tree.kraken import latest_indicator_values, latest_snapshot


class IndicatorClient(Protocol):
    """Abstract interface to fetch indicator snapshots from kolaBiTree."""

    def fetch_snapshot(self, symbol: str) -> Dict[str, Any]: ...


@dataclass
class DummyIndicatorClient:
    """In-memory indicator store used until kolaBiTree is live."""

    snapshot: Dict[str, Any] | None = None

    def fetch_snapshot(self, symbol: str) -> Dict[str, Any]:
        return self.snapshot or {"symbol": symbol, "indicators": {}}


@dataclass
class KrakenDbIndicatorClient:
    """Lit les indicateurs compacts produits par le service KrakenTree."""

    db_url: str = "sqlite:///pub-futures-demo.sqlite"
    exchange: str = "kraken"
    environment: str = "demo"
    market_type: str = "futures"

    def fetch_snapshot(self, symbol: str) -> Dict[str, Any]:
        """Lit le dernier snapshot stocke pour une paire Kraken.

        Args:
            symbol: Nom de paire Kraken stocke par le service, par exemple
                ``BTC/USD``. Le filtre est important car la meme DB peut
                recevoir plusieurs paires.

        Returns:
            Mapping plat avec les indicateurs compacts, ou payload vide si la
            DB locale ne contient encore aucune ligne pour cette paire.
        """
        engine = create_engine(self.db_url)
        with Session(engine) as session:
            # Commentaire FR: le client ouvre une session courte pour ne pas
            # garder de verrou de lecture pendant que KrakenTree ecrit.
            result = latest_snapshot(
                session,
                symbol,
                self.exchange,
                self.environment,
                self.market_type,
            )
            if not result:
                return {"symbol": symbol, "indicators": {}}
            indicators = latest_indicator_values(
                session,
                symbol,
                self.exchange,
                self.environment,
                self.market_type,
            )
            return {
                "symbol": symbol,
                "exchange": result.exchange,
                "environment": result.environment,
                "market_type": result.market_type,
                "avg_ask": result.avg_ask,
                "avg_bid": result.avg_bid,
                "best_ask": result.best_ask,
                "best_bid": result.best_bid,
                "spread": result.spread,
                "mid_price": result.mid_price,
                "imbalance": result.imbalance,
                "recorded_at": result.local_timestamp.isoformat(),
                "source_timestamp": result.source_timestamp.isoformat()
                if result.source_timestamp
                else None,
                "indicators": {
                    name: indicator.value for name, indicator in indicators.items()
                },
            }
