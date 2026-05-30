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

from kolabi.bot.domain import OrderPairSpec, PairCycleState, Side
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


def pair_window_has_ended(
    pair: OrderPairSpec,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    """Return true after the pair launch-relative window has ended."""
    elapsed_minutes = (now - launched_at).total_seconds() / 60.0
    return elapsed_minutes > pair.window.end_minutes


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


def executable_head_reference_price(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[str, float]:
    """Return the executable public reference for head placement conditions."""
    if pair.head.side == Side.BUY:
        return "ask", _price_or_fallback(market.best_ask, market.mid_price)
    return "bid", _price_or_fallback(market.best_bid, market.mid_price)


def head_price_condition_satisfied(
    pair_state: PairCycleState,
    reference_price: Decimal | int | float | str,
) -> bool:
    """Evaluate the head prix interval against current and baseline reference."""
    pair = pair_state.pair
    current = to_decimal(reference_price)
    low, high = (to_decimal(pair.head_price[0]), to_decimal(pair.head_price[1]))
    price_type = (pair.head_price_type or "").lower()
    amount_type = pair.amount_type.lower()
    if "pa" in price_type or "pa" in amount_type:
        return low <= current <= high

    baseline = pair_state.head_trigger_reference_price
    if baseline is None or baseline <= 0:
        return False
    if "p%" in price_type or "p%" in amount_type:
        value = (current - baseline) * Decimal("100") / baseline
    else:
        value = current - baseline
    return low <= value <= high


def head_price_condition_needs_baseline(pair: OrderPairSpec) -> bool:
    price_type = (pair.head_price_type or "").lower()
    amount_type = pair.amount_type.lower()
    return not ("pa" in price_type or "pa" in amount_type)


def tail_trigger_source(order_type: str) -> str:
    """Resolve abstract trigger source from legacy tail order suffixes."""
    normalized = (order_type or "").strip().lower()
    reducible = normalized[:-1] if normalized.endswith("-") else normalized
    if reducible.endswith("f"):
        return "mark"
    if reducible.endswith("i"):
        return "index"
    if reducible:
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
