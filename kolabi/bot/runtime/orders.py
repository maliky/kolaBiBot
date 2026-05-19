"""Temporary bridge to legacy order condition classes."""

from kolabi.runtime.legacy.kola.orders.hookorder import HookOrder  # type: ignore
from kolabi.runtime.legacy.kola.orders.ordercond import OrderConditionned  # type: ignore
from kolabi.runtime.legacy.kola.orders.trailstop import TrailStop  # type: ignore

__all__ = ["OrderConditionned", "HookOrder", "TrailStop"]
