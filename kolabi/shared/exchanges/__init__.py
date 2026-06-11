"""Utilities to load exchange adapters."""

from importlib import import_module
from types import ModuleType
from typing import Type, cast

from kolabi.shared.core.types import ExchangeABC

DEFAULT_MARKET_TYPE = "futures"
_SUPPORTED_MARKET_TYPES_BY_EXCHANGE = {
    "binance": frozenset({"futures", "spot", "margin", "isolated_margin"}),
    "kraken": frozenset({"futures", "spot", "margin"}),
    "bitmex": frozenset({"futures", "spot"}),
}


def get_adapter(name: str, market_type: str = DEFAULT_MARKET_TYPE) -> Type[ExchangeABC]:
    """Return the adapter class for the given exchange name."""
    normalised_name = name.strip().lower()
    normalised_market_type = (market_type or DEFAULT_MARKET_TYPE).strip().lower()
    supported_markets = _SUPPORTED_MARKET_TYPES_BY_EXCHANGE.get(normalised_name)
    if (
        supported_markets is not None
        and normalised_market_type not in supported_markets
    ):
        supported = ", ".join(sorted(supported_markets))
        raise ImportError(
            f"Exchange adapter '{normalised_name}' does not support market type "
            f"'{normalised_market_type}'. Supported market types: {supported}"
        )
    module_name = f"kolabi.shared.exchanges.{normalised_name}_adapter"
    try:
        module: ModuleType = import_module(module_name)
    except Exception as exc:
        raise ImportError(
            f"Failed to load exchange adapter '{normalised_name}': {exc}"
        ) from exc
    if normalised_name == "binance":
        return cast(
            Type[ExchangeABC],
            _market_adapter_class(module, normalised_market_type, "Binance"),
        )
    if normalised_name == "kraken":
        return cast(
            Type[ExchangeABC],
            _market_adapter_class(module, normalised_market_type, "Kraken"),
        )
    if normalised_name == "bitmex":
        return cast(
            Type[ExchangeABC],
            _market_adapter_class(module, normalised_market_type, "Bitmex"),
        )
    if normalised_market_type != DEFAULT_MARKET_TYPE:
        raise ImportError(
            f"Exchange adapter '{normalised_name}' does not support market type "
            f"'{normalised_market_type}'"
        )
    if not hasattr(module, "BinanceAdapter") and not hasattr(module, "Adapter"):
        raise ImportError(f"Module '{module_name}' does not define an adapter class")
    cls = getattr(module, "Adapter", None) or getattr(module, "BinanceAdapter")
    return cast(Type[ExchangeABC], cls)


def _market_adapter_class(
    module: ModuleType,
    market_type: str,
    exchange_prefix: str,
) -> Type[ExchangeABC]:
    adapter_name_by_market = {
        "futures": f"{exchange_prefix}FuturesAdapter",
        "spot": f"{exchange_prefix}SpotAdapter",
        "margin": f"{exchange_prefix}MarginAdapter",
        "isolated_margin": f"{exchange_prefix}MarginAdapter",
    }
    class_name = adapter_name_by_market.get(market_type)
    if class_name is None:
        raise ImportError(
            f"{exchange_prefix} adapter does not support market type '{market_type}'"
        )
    cls = getattr(module, class_name, None)
    if cls is None and market_type == DEFAULT_MARKET_TYPE:
        cls = getattr(module, "Adapter", None) or getattr(
            module,
            f"{exchange_prefix}Adapter",
            None,
        )
    if cls is None:
        raise ImportError(f"Module '{module.__name__}' does not define {class_name}")
    return cast(Type[ExchangeABC], cls)
__all__ = ["get_adapter"]
