from __future__ import annotations

from dataclasses import dataclass

import pytest

from kolabi.bot.exchange_routes import (
    SUPPORTED_CODE_ROUTES,
    SUPPORTED_EXCHANGE_MARKET_TYPES,
    default_symbol_for_route,
    exchange_supports_market_type,
    pair_route,
    parse_exchange_code,
)
from kolabi.shared.exchanges import get_adapter
from kolabi.shared.exchanges.binance_adapter import (
    BinanceFuturesAdapter,
    BinanceMarginAdapter,
    BinanceSpotAdapter,
)
from kolabi.shared.exchanges.bitmex_adapter import BitmexFuturesAdapter, BitmexSpotAdapter
from kolabi.shared.exchanges.kraken_adapter import (
    KrakenFuturesAdapter,
    KrakenMarginAdapter,
    KrakenSpotAdapter,
)


@dataclass(frozen=True)
class _Pair:
    name: str
    exchange: str
    market_type: str
    symbol: str


@pytest.mark.parametrize(
    ("code", "exchange", "market_type", "adapter_cls"),
    [
        ("KRK", "kraken", "futures", KrakenFuturesAdapter),
        ("KRKF", "kraken", "futures", KrakenFuturesAdapter),
        ("KRKS", "kraken", "spot", KrakenSpotAdapter),
        ("KRKM", "kraken", "margin", KrakenMarginAdapter),
        ("BIN", "binance", "futures", BinanceFuturesAdapter),
        ("BINF", "binance", "futures", BinanceFuturesAdapter),
        ("BINS", "binance", "spot", BinanceSpotAdapter),
        ("BINM", "binance", "margin", BinanceMarginAdapter),
        ("BINI", "binance", "isolated_margin", BinanceMarginAdapter),
        ("BMX", "bitmex", "futures", BitmexFuturesAdapter),
        ("BMXF", "bitmex", "futures", BitmexFuturesAdapter),
        ("BMXS", "bitmex", "spot", BitmexSpotAdapter),
        ("BTX", "bitmex", "futures", BitmexFuturesAdapter),
        ("BTXF", "bitmex", "futures", BitmexFuturesAdapter),
        ("BTXS", "bitmex", "spot", BitmexSpotAdapter),
    ],
)
def test_supported_strategy_codes_have_order_adapter(
    code: str,
    exchange: str,
    market_type: str,
    adapter_cls: type,
) -> None:
    assert parse_exchange_code(code) == (exchange, market_type)
    assert exchange_supports_market_type(exchange, market_type)
    assert get_adapter(exchange, market_type) is adapter_cls


def test_route_code_matrix_matches_supported_exchange_markets() -> None:
    assert SUPPORTED_CODE_ROUTES == SUPPORTED_EXCHANGE_MARKET_TYPES


@pytest.mark.parametrize(
    ("exchange", "market_type", "symbol"),
    [
        ("kraken", "futures", "PI_XBTUSD"),
        ("kraken", "spot", "XBT/USD"),
        ("kraken", "margin", "XBT/USD"),
        ("binance", "futures", "BTCUSDT"),
        ("binance", "spot", "BTCUSDT"),
        ("binance", "margin", "BTCUSDT"),
        ("binance", "isolated_margin", "BTCUSDT"),
        ("bitmex", "futures", "XBTUSD"),
        ("bitmex", "spot", "XBT_USDT"),
    ],
)
def test_default_symbol_for_route_matches_operator_defaults(
    exchange: str,
    market_type: str,
    symbol: str,
) -> None:
    assert default_symbol_for_route(exchange, market_type) == symbol


@pytest.mark.parametrize(
    ("exchange", "market_type"),
    [
        ("kraken", "isolated_margin"),
        ("bitmex", "margin"),
        ("bitmex", "isolated_margin"),
    ],
)
def test_adapter_loader_rejects_unsupported_exchange_market_lanes(
    exchange: str,
    market_type: str,
) -> None:
    with pytest.raises(ImportError, match="does not support market type"):
        get_adapter(exchange, market_type)


def test_pair_route_rejects_unsupported_market_lane() -> None:
    pair = _Pair(
        name="BMX_MARGIN",
        exchange="bitmex",
        market_type="margin",
        symbol="XBTUSD",
    )

    with pytest.raises(ValueError, match="only supported for Binance or Kraken"):
        pair_route(pair, default_exchange="kraken", default_symbol="PI_XBTUSD")
