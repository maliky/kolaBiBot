# mypy: ignore-errors
"""Typed wrappers around legacy runtime classes.

Purpose: expose lightweight typed entrypoints over mutable legacy objects while
migration to shared typed runtime boundaries is in progress.
Inputs: legacy runtime instances plus typed command/order objects.
Outputs: typed-ish dispatch/evaluation results suitable for new call sites.
Side effects: delegated to wrapped objects (network/order submission/queue IO).
Important types: `RuntimeCommand`, `OrderDict`, `BrokerReply`.
Role: boundary adapter.
Transitional: yes, this module is an explicit migration shim.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kolabi.runtime.kola.bargain import KolaBargain
from kolabi.runtime.kola.chronos import Chronos
from kolabi.runtime.kola.orders.condition import Condition
from kolabi.runtime.kola.orders.hookorder import HookOrder
from kolabi.runtime.kola.orders.ordercond import OrderConditionned
from kolabi.runtime.kola.orders.trailstop import TrailStop
from kolabi.runtime.kola.price import PriceObj
from kolabi.runtime.kola.utils import orderfunc
from kolabi.shared.core.runtime_types import BrokerReply, OrderDict, RuntimeCommand


@dataclass(frozen=True)
class ChronosTypedAdapter:
    inner: Chronos

    def submit(self, command: RuntimeCommand) -> BrokerReply | dict[str, Any]:
        payload = command.order or {}
        return self.inner.build_runtime_command(dict(payload), str(command.symbol)).order or {}


@dataclass(frozen=True)
class BargainTypedAdapter:
    inner: KolaBargain

    def price(self, price_type: str, side: str | None = None) -> float:
        return float(self.inner.prices(price_type, side=side))


@dataclass(frozen=True)
class ConditionTypedAdapter:
    inner: Condition

    def evaluate(self) -> bool:
        return bool(self.inner.is_(True))


@dataclass(frozen=True)
class OrderConditionTypedAdapter:
    inner: OrderConditionned

    def dispatch(self, order: OrderDict | None = None) -> BrokerReply | bool:
        return self.inner.send_order(order)


@dataclass(frozen=True)
class HookOrderTypedAdapter:
    inner: HookOrder

    def should_release(self) -> bool:
        return bool(self.inner.hasbeen_hooked())


@dataclass(frozen=True)
class TrailStopTypedAdapter:
    inner: TrailStop

    def amend(self, order_id: str) -> Any:
        return self.inner.amend_stop_price(order_id)


@dataclass(frozen=True)
class PriceTypedAdapter:
    inner: PriceObj

    def update(self, *, price: float, ref_price: float) -> None:
        self.inner.update_to(price=price, refPrice=ref_price)


@dataclass(frozen=True)
class OrderFuncTypedAdapter:
    @staticmethod
    def normalize(order: OrderDict) -> OrderDict:
        return orderfunc.normalize_order_dict(order)
