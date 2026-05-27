"""Pure pricing and time-window helpers for pair-cycle decisions.

Purpose: compute head prices from market snapshots and evaluate absolute pair
activation windows from strategy launch time.
Inputs: `OrderPairSpec`, `PublicMarketState`, strategy launch/current times.
Outputs: deterministic activation booleans and optional head limit prices.
Side effects: none.
Important types: `OrderPairSpec`, `Side`, `PublicMarketState`.
Role: pure logic.
Transitional: yes, extracted from `pair_cycle.py` while old runtime layers remain.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from kolabi.bot.domain import OrderPairSpec, Side
from kolabi.shared.core.runtime_types import decimal_to_float, to_decimal


class MarketLike(Protocol):
    @property
    def best_bid(self) -> float | None: ...

    @property
    def best_ask(self) -> float | None: ...

    @property
    def mid_price(self) -> float | None: ...
    @property
    def last_price(self) -> float | None: ...
    @property
    def mark_price(self) -> float | None: ...
    @property
    def index_price(self) -> float | None: ...


def pair_window_is_open(
    pair: OrderPairSpec,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    """Return true when current time is inside the pair launch-relative window."""
    elapsed_minutes = (now - launched_at).total_seconds() / 60.0
    return pair.window.start_minutes <= elapsed_minutes <= pair.window.end_minutes


def resolve_head_price(pair: OrderPairSpec, market: MarketLike) -> float | None:
    """Resolve the head order price from pair configuration and live market state."""
    order_type = pair.head.order_type.replace("_", "").replace("-", "").lower()
    if order_type in {"m", "market"}:
        return None
    reference = to_decimal(reference_price(pair.head.side, market))
    lower, upper = pair.head_price
    if "pA" in pair.amount_type:
        return decimal_to_float(lower if pair.head.side == Side.BUY else upper)
    if "p%" in pair.amount_type:
        offset = to_decimal(lower if pair.head.side == Side.BUY else upper)
        return decimal_to_float(reference * (Decimal("1") + offset / Decimal("100")))
    offset = to_decimal(lower if pair.head.side == Side.BUY else upper)
    return decimal_to_float(reference + offset)


def reference_price(side: Side, market: MarketLike) -> float:
    """Return the reference side-aware market price used for relative pricing."""
    if side == Side.BUY:
        return market.best_bid or market.mid_price or 0.0
    return market.best_ask or market.mid_price or 0.0


def tail_trigger_source(order_type: str) -> str:
    """Resolve abstract trigger source from legacy tail order suffixes."""
    normalized = (order_type or "").strip()
    if normalized.endswith("f") or normalized.endswith("f-"):
        return "mark"
    if normalized.endswith("i") or normalized.endswith("i-"):
        return "index"
    if normalized.endswith("-"):
        return "last"
    return "book"


def tail_reference_price(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[str, float]:
    """Return (source, reference) aligned with tail trigger semantics."""
    source = tail_trigger_source(pair.tail.order_type)
    market_any = market_as_any(market)
    if source == "last":
        return source, _price_or_fallback(
            getattr(market_any, "last_price", None), market.mid_price
        )
    if source == "mark":
        return source, _price_or_fallback(
            getattr(market_any, "mark_price", None), market.mid_price
        )
    if source == "index":
        return source, _price_or_fallback(
            getattr(market_any, "index_price", None), market.mid_price
        )
    return "book", reference_price(pair.head.side, market)


def _price_or_fallback(primary: float | None, fallback: float | None) -> float:
    if primary is not None and primary > 0:
        return primary
    if fallback is not None and fallback > 0:
        return fallback
    return 0.0


def market_as_any(market: MarketLike) -> Any:
    return market
