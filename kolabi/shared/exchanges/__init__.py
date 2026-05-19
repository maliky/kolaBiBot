"""Utilities to load exchange adapters."""

from importlib import import_module
from types import ModuleType
from typing import Type, cast

from kolabi.shared.core.types import ExchangeABC


def get_adapter(name: str) -> Type[ExchangeABC]:
    """Return the adapter class for the given exchange name."""
    module_name = f"kolabi.shared.exchanges.{name}_adapter"
    try:
        module: ModuleType = import_module(module_name)
    except Exception as exc:
        raise ImportError(f"Failed to load exchange adapter '{name}': {exc}") from exc
    if not hasattr(module, "BinanceAdapter") and not hasattr(module, "Adapter"):
        raise ImportError(f"Module '{module_name}' does not define an adapter class")
    cls = getattr(module, "Adapter", None) or getattr(module, "BinanceAdapter")
    return cast(Type[ExchangeABC], cls)
__all__ = ["get_adapter"]
