"""Exchange route helpers for strategy rows and runtime command routing."""

from __future__ import annotations

from dataclasses import dataclass

MARKET_TYPE_FUTURES = "futures"
MARKET_TYPE_SPOT = "spot"
MARKET_TYPE_MARGIN = "margin"
MARKET_TYPE_ISOLATED_MARGIN = "isolated_margin"
DEFAULT_MARKET_TYPE = MARKET_TYPE_FUTURES
EXCHANGE_MARKET_TYPES: dict[str, frozenset[str]] = {
    "kraken": frozenset(
        {
            MARKET_TYPE_FUTURES,
            MARKET_TYPE_SPOT,
            MARKET_TYPE_MARGIN,
        }
    ),
    "binance": frozenset(
        {
            MARKET_TYPE_FUTURES,
            MARKET_TYPE_SPOT,
            MARKET_TYPE_MARGIN,
            MARKET_TYPE_ISOLATED_MARGIN,
        }
    ),
    "bitmex": frozenset(
        {
            MARKET_TYPE_FUTURES,
            MARKET_TYPE_SPOT,
        }
    ),
}
SUPPORTED_EXCHANGES = frozenset(EXCHANGE_MARKET_TYPES)
SUPPORTED_MARKET_TYPES = frozenset(
    market_type
    for market_types in EXCHANGE_MARKET_TYPES.values()
    for market_type in market_types
)
SUPPORTED_EXCHANGE_MARKET_TYPES = frozenset(
    {
        (exchange, market_type)
        for exchange, market_types in EXCHANGE_MARKET_TYPES.items()
        for market_type in market_types
    }
)

_DEFAULT_SYMBOLS_BY_ROUTE: dict[tuple[str, str], str] = {
    ("kraken", MARKET_TYPE_FUTURES): "PI_XBTUSD",
    ("kraken", MARKET_TYPE_SPOT): "XBT/USD",
    ("kraken", MARKET_TYPE_MARGIN): "XBT/USD",
    ("binance", MARKET_TYPE_FUTURES): "BTCUSDT",
    ("binance", MARKET_TYPE_SPOT): "BTCUSDT",
    ("binance", MARKET_TYPE_MARGIN): "BTCUSDT",
    ("binance", MARKET_TYPE_ISOLATED_MARGIN): "BTCUSDT",
    ("bitmex", MARKET_TYPE_FUTURES): "XBTUSD",
    ("bitmex", MARKET_TYPE_SPOT): "XBT_USDT",
}


def market_types_for_exchange(exchange: str) -> frozenset[str]:
    """Return supported market lanes for an exchange."""

    return EXCHANGE_MARKET_TYPES.get(exchange, frozenset())


def route_codes_for_market(exchange: str, market_type: str) -> tuple[str, ...]:
    """Return advertised TSV route codes for one exchange/market lane."""

    exchange_name = str(exchange or "").strip().lower()
    route_market_type = str(market_type or DEFAULT_MARKET_TYPE).strip().lower()
    if not exchange_supports_market_type(exchange_name, route_market_type):
        return ()
    codes: list[str] = []
    for code in sorted(SUPPORTED_ROUTE_CODES):
        code_exchange, code_market_type = parse_exchange_code(code)
        if code_exchange == exchange_name and code_market_type == route_market_type:
            codes.append(code)
    return tuple(codes)


def exchange_supports_market_type(exchange: str, market_type: str) -> bool:
    """Return whether a canonical exchange supports a canonical market lane."""

    return market_type in market_types_for_exchange(exchange)


def default_symbol_for_route(
    exchange: str,
    market_type: str = DEFAULT_MARKET_TYPE,
) -> str:
    """Return the operator-facing default symbol for one exchange route."""

    exchange_name = str(exchange or "").strip().lower()
    if exchange_name not in SUPPORTED_EXCHANGES:
        raise ValueError(
            f"Unsupported exchange '{exchange}'. Supported exchanges: "
            + ", ".join(sorted(SUPPORTED_EXCHANGES))
        )
    route_market_type = str(market_type or DEFAULT_MARKET_TYPE).strip().lower()
    if route_market_type not in SUPPORTED_MARKET_TYPES:
        raise ValueError(
            f"Unsupported market type '{route_market_type}' for exchange "
            f"'{exchange_name}'"
        )
    if not exchange_supports_market_type(exchange_name, route_market_type):
        reason = unsupported_market_message(exchange_name, route_market_type)
        raise ValueError(
            f"Market type '{route_market_type}' {reason} for exchange "
            f"'{exchange_name}'"
        )
    return _DEFAULT_SYMBOLS_BY_ROUTE[(exchange_name, route_market_type)]


_MARKET_TYPE_LABELS: dict[str, str] = {
    MARKET_TYPE_FUTURES: "Futures",
    MARKET_TYPE_SPOT: "Spot",
    MARKET_TYPE_MARGIN: "Margin",
    MARKET_TYPE_ISOLATED_MARGIN: "Isolated Margin",
}

_SUPPORTED_MARKETS_TEXT = ", ".join(
    f"{exchange}="
    + "/".join(
        _MARKET_TYPE_LABELS.get(market_type, market_type)
        for market_type in sorted(market_types)
    )
    for exchange, market_types in sorted(EXCHANGE_MARKET_TYPES.items())
)

_SUPPORTED_CODE_HINT = (
    "Use KRK/KRKF for Kraken Futures, KRKS for Kraken Spot, "
    "KRKM for Kraken Margin, BIN/BINF for Binance Futures, "
    "BINS for Binance Spot, BINM for Binance Cross Margin, "
    "BINI for Binance Isolated Margin, BMX/BMXF or BTX/BTXF for BitMEX Futures, "
    "or BMXS/BTXS for BitMEX Spot."
)


SUPPORTED_ROUTE_CODES = frozenset(
    {
        "KRK",
        "KRKF",
        "KRKS",
        "KRKM",
        "BIN",
        "BINF",
        "BINS",
        "BINM",
        "BINI",
        "BMX",
        "BMXF",
        "BMXS",
        "BTX",
        "BTXF",
        "BTXS",
    }
)

_DIRECT_ROUTE_CODES: dict[str, tuple[str, str]] = {
    "KRKF": ("kraken", MARKET_TYPE_FUTURES),
    "KRAKENF": ("kraken", MARKET_TYPE_FUTURES),
    "KRKS": ("kraken", MARKET_TYPE_SPOT),
    "KRAKENS": ("kraken", MARKET_TYPE_SPOT),
    "KRKM": ("kraken", MARKET_TYPE_MARGIN),
    "KRAKENM": ("kraken", MARKET_TYPE_MARGIN),
    "BINF": ("binance", MARKET_TYPE_FUTURES),
    "BINANCEF": ("binance", MARKET_TYPE_FUTURES),
    "BINS": ("binance", MARKET_TYPE_SPOT),
    "BINANCES": ("binance", MARKET_TYPE_SPOT),
    "BINM": ("binance", MARKET_TYPE_MARGIN),
    "BINANCEM": ("binance", MARKET_TYPE_MARGIN),
    "BINI": ("binance", MARKET_TYPE_ISOLATED_MARGIN),
    "BINANCEI": ("binance", MARKET_TYPE_ISOLATED_MARGIN),
    "BMXF": ("bitmex", MARKET_TYPE_FUTURES),
    "BTXF": ("bitmex", MARKET_TYPE_FUTURES),
    "BITMEXF": ("bitmex", MARKET_TYPE_FUTURES),
    "BMXS": ("bitmex", MARKET_TYPE_SPOT),
    "BTXS": ("bitmex", MARKET_TYPE_SPOT),
    "BITMEXS": ("bitmex", MARKET_TYPE_SPOT),
}

SUPPORTED_CODE_ROUTES = frozenset(_DIRECT_ROUTE_CODES.values()) | frozenset(
    {
        ("kraken", MARKET_TYPE_FUTURES),
        ("binance", MARKET_TYPE_FUTURES),
        ("bitmex", MARKET_TYPE_FUTURES),
    }
)

if not SUPPORTED_CODE_ROUTES <= SUPPORTED_EXCHANGE_MARKET_TYPES:
    raise RuntimeError("route code matrix includes unsupported exchange market lanes")

_UNSUPPORTED_MARKET_MESSAGES: dict[str, str] = {
    MARKET_TYPE_ISOLATED_MARGIN: "only supported for Binance",
    MARKET_TYPE_MARGIN: "only supported for Binance or Kraken",
    MARKET_TYPE_SPOT: "only supported for Binance, Kraken, or BitMEX",
}


def unsupported_market_message(exchange: str, market_type: str) -> str:
    """Return the operator-facing reason for an unsupported route."""

    return _UNSUPPORTED_MARKET_MESSAGES.get(
        market_type,
        f"is not supported for exchange '{exchange}'",
    )


@dataclass(frozen=True, order=True)
class ExchangeRoute:
    exchange: str
    market_type: str
    symbol: str

    @property
    def label(self) -> str:
        return f"{self.exchange}:{self.market_type}:{self.symbol}"


_EXCHANGE_CODE_ALIASES: dict[str, str] = {
    "KRK": "kraken",
    "KRAKEN": "kraken",
    "BIN": "binance",
    "BINANCE": "binance",
    "BMX": "bitmex",
    "BTX": "bitmex",
    "BITMEX": "bitmex",
}


def parse_exchange_code(raw: str | None) -> tuple[str | None, str | None]:
    """Parse a TSV exchange code into canonical exchange and market type."""

    text = str(raw or "").strip()
    if not text:
        return None, None
    compact = text.upper()
    direct = _DIRECT_ROUTE_CODES.get(compact)
    if direct is not None:
        return direct
    suffix = compact[-1:] if compact[-1:] in {"F", "S"} else ""
    base = compact[:-1] if suffix else compact
    exchange = _EXCHANGE_CODE_ALIASES.get(base)
    if exchange is None:
        lower = text.lower()
        if lower in SUPPORTED_EXCHANGES:
            exchange = lower
        else:
            raise ValueError(
                f"Unsupported exchange code '{text}'. {_SUPPORTED_CODE_HINT}"
            )
    market_type = MARKET_TYPE_SPOT if suffix == "S" else DEFAULT_MARKET_TYPE
    if not exchange_supports_market_type(exchange, market_type):
        reason = unsupported_market_message(exchange, market_type)
        raise ValueError(
            f"Market type '{market_type}' {reason} for exchange code '{text}'. "
            f"{_SUPPORTED_CODE_HINT}"
        )
    return exchange, market_type


def normalise_exchange_name(raw: str) -> str:
    """Return a canonical exchange name from CLI names or TSV codes."""

    exchange, market_type = parse_exchange_code(raw)
    if exchange is None:
        exchange = str(raw).strip().lower()
    if market_type not in {None, DEFAULT_MARKET_TYPE}:
        raise ValueError(f"Unsupported market type '{market_type}' for exchange '{raw}'")
    if exchange not in SUPPORTED_EXCHANGES:
        raise ValueError(
            f"Unsupported exchange '{raw}'. Supported exchanges: "
            + ", ".join(sorted(SUPPORTED_EXCHANGES))
        )
    return exchange


def pair_route(
    pair: object,
    *,
    default_exchange: str,
    default_symbol: str,
    default_market_type: str = DEFAULT_MARKET_TYPE,
) -> ExchangeRoute:
    """Resolve a pair object to a complete exchange/market/symbol route."""

    symbol = str(getattr(pair, "symbol", "") or "").strip() or default_symbol
    exchange = str(getattr(pair, "exchange", "") or "").strip()
    if not exchange:
        exchange = normalise_exchange_name(default_exchange)
    else:
        exchange = normalise_exchange_name(exchange)
    market_type = str(
        getattr(pair, "market_type", "") or default_market_type
    ).strip().lower()
    if market_type not in SUPPORTED_MARKET_TYPES:
        raise ValueError(
            f"Unsupported market type '{market_type}' for pair "
            f"'{getattr(pair, 'name', '-')}'."
        )
    if not exchange_supports_market_type(exchange, market_type):
        reason = unsupported_market_message(exchange, market_type)
        raise ValueError(
            f"Market type '{market_type}' {reason} pair "
            f"'{getattr(pair, 'name', '-')}'."
        )
    return ExchangeRoute(exchange=exchange, market_type=market_type, symbol=symbol)
