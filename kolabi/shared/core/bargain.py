from __future__ import annotations

from typing import Any, cast

from pandas import DataFrame

from kolabi.runtime.legacy.kola.bargain import LegacyBargain
from kolabi.runtime.legacy.kola.utils.constantes import EXECOLS
from kolabi.runtime.legacy.kola.utils.general import round_sprice
from kolabi.shared.config import ExchangeConfig, load_exchange_config
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.runtime_state import KrakenRuntimeStateClient


class Bargain(LegacyBargain):
    """Legacy bargain surface backed by the new shared exchange adapters."""

    # > Since we are using KrankenRuntime we should probably rename this class KrakenBargain
    def __init__(self, exchange: str, config: ExchangeConfig) -> None:
        self.exchange = exchange.lower()
        self.config = config
        adapter_cls = get_adapter(self.exchange)
        crypto_api = adapter_cls(
            api_key=config.api_key,
            api_secret=config.api_secret,
            base_url=config.base_url,
            symbol=config.symbol,
            **config.adapter_kwargs,
        )
        super().__init__(
            live=config.adapter_kwargs.get("environment") == "live",
            symbol=config.symbol,
            dbo=crypto_api,
            trading_plateform=self.exchange,
        )
        # dbo is for dummy bargain, we do not need it here
        self.dbo = None
        self._runtime_state: KrakenRuntimeStateClient | None = None
        if self.exchange == "kraken":
            public_db_url = str(config.adapter_kwargs.get("public_db_url", "") or "")
            account_db_url = str(config.adapter_kwargs.get("account_db_url", "") or "")
            environment = str(config.adapter_kwargs.get("environment", "demo"))
            if public_db_url and account_db_url:
                self._runtime_state = KrakenRuntimeStateClient(
                    market_db_url=public_db_url,
                    account_db_url=account_db_url,
                    symbol=config.symbol,
                    exchange=self.exchange,
                    environment=environment,
                    market_type="futures",
                )

    @classmethod
    def from_env(cls, exchange: str, symbol: str, **kwargs: object) -> "Bargain":
        config = load_exchange_config(
            exchange,
            symbol=symbol,
            **cast(dict[str, Any], kwargs),
        )
        return cls(exchange, config)

    def describe(self) -> str:
        """Return a short exchange:symbol identifier."""
        return f"{self.exchange}:{self.symbol}"

    def get_most_recent_settlement_price(self):
        """Use the adapter instrument payload instead of the BitMEX settlement API."""
        if self.exchange == "kraken" and self._runtime_state is not None:
            market = self._runtime_state.fetch_market_state(self.symbol)
            if market.ready:
                return market.indicators.get("index_price", market.mid_price)
        instrument = self.crypto_api.instrument(self.symbol)
        return instrument.get("indicativeSettlePrice") or instrument.get("markPrice")

    def prices(
        self,
        typeprice: str | None = None,
        side: str = "buy",
        symbol_: str | None = None,
        force_live: bool = False,
    ) -> Any:
        """Return Kraken prices from the local market DB when available.

        The legacy runtime drives conditions and trailing behaviour through this
        method. For Kraken Futures we prefer the local public DB so strategy
        triggers follow the same persisted market view as the rest of the stack.
        """
        if self.exchange != "kraken" or force_live or self._runtime_state is None:
            return super().prices(typeprice, side, symbol_, force_live)
        target_symbol = self.symbol if symbol_ is None else symbol_
        market = self._runtime_state.fetch_market_state(target_symbol)
        if not market.ready or market.best_bid is None or market.best_ask is None:
            return super().prices(typeprice, side, symbol_, force_live)
        prices = self._db_prices_map(market)
        return self._price_from_db_map(prices, typeprice, side, target_symbol)

    def _db_prices_map(self, market: Any) -> dict[str, float]:
        """Build a legacy-compatible instrument price map from the market DB."""
        mark_price = market.indicators.get("mark_price", market.mid_price)
        index_price = market.indicators.get("index_price", market.mid_price)
        impact_bid = market.avg_bid if market.avg_bid is not None else market.best_bid
        impact_ask = market.avg_ask if market.avg_ask is not None else market.best_ask
        impact_mid = (impact_bid + impact_ask) / 2 if impact_bid and impact_ask else market.mid_price
        return {
            "askPrice": float(market.best_ask),
            "bidPrice": float(market.best_bid),
            "midPrice": float(market.mid_price or ((market.best_bid + market.best_ask) / 2)),
            "markPrice": float(mark_price or market.mid_price),
            "fairPrice": float(mark_price or market.mid_price),
            "indicativeSettlePrice": float(index_price or market.mid_price),
            "indexPrice": float(index_price or market.mid_price),
            "lastPrice": float(market.mid_price or ((market.best_bid + market.best_ask) / 2)),
            "lastPriceProtected": float(market.mid_price or ((market.best_bid + market.best_ask) / 2)),
            "impactBidPrice": float(impact_bid or market.best_bid),
            "impactAskPrice": float(impact_ask or market.best_ask),
            "impactMidPrice": float(impact_mid or market.mid_price),
        }

    def _price_from_db_map(
        self,
        prices: dict[str, float],
        typeprice: str | None,
        side: str,
        symbol: str,
    ) -> Any:
        """Mirror the legacy `prices` semantics against a DB-backed price map."""
        typeprice = "" if typeprice is None else typeprice
        ret: float | None = None
        if typeprice == "delta":
            ret = prices["askPrice"] - prices["bidPrice"]
        elif typeprice.lower() == "indexprice":
            ret = prices["indexPrice"]
        elif typeprice.lower() == "lastprice":
            ret = prices["askPrice"] if side == "buy" else prices["bidPrice"]
        elif typeprice == "market_maker":
            ret = prices["bidPrice"] if side == "buy" else prices["askPrice"]
        elif typeprice.lower() == "lastmidprice":
            ret = prices["midPrice"]
        elif typeprice.lower() in {"market", "market_price", "markprice"}:
            ret = prices["markPrice"]
        elif typeprice == "ref_delta":
            ret = prices["midPrice"] - prices["indexPrice"]
        elif typeprice:
            ret = prices[self.camelCase_price(typeprice)]
        return prices if ret is None else round_sprice(ret, symbol)

    def execution(self, clOrdID_: str | None = None) -> DataFrame:
        """Return a legacy-shaped execution table for Kraken without `ws.data`.

        The old engine expects BitMEX websocket state under `crypto_api.ws`.
        Kraken does not expose that shape here, so we rebuild the execution
        table from the adapter `exec_orders()` surface, which already uses the
        private DB as source of truth when available.
        """
        if self.exchange != "kraken":
            return super().execution(clOrdID_)
        rows = list(self.crypto_api.exec_orders())
        if not rows:
            return DataFrame(columns=EXECOLS)
        df = DataFrame(rows)
        for column in EXECOLS:
            if column not in df.columns:
                df[column] = None
        if clOrdID_ is not None:
            df = df[df["clOrdID"] == clOrdID_]
        if "transactTime" in df.columns:
            df = df.sort_values("transactTime", kind="stable")
        return df.loc[:, EXECOLS].reset_index(drop=True)

    def minimum_order_quantity(self) -> float:
        """Return the active instrument minimum quantity when the adapter exposes it."""
        helper = getattr(self.crypto_api, "minimum_order_quantity", None)
        if callable(helper):
            return float(helper(self.symbol))
        return 30.0
