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

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from kolabi.bot.domain import OrderPairSpec, PairCycleState, Side
from kolabi.bot.order_codes import order_price_source, parse_order_code
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
    elapsed_minutes = _elapsed_minutes(launched_at=launched_at, now=now)
    return pair.window.start_minutes <= elapsed_minutes <= pair.window.end_minutes


def pair_window_has_ended(
    pair: OrderPairSpec,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    """Return true after the pair launch-relative window has ended."""
    elapsed_minutes = _elapsed_minutes(launched_at=launched_at, now=now)
    return elapsed_minutes > pair.window.end_minutes


def _elapsed_minutes(*, launched_at: datetime, now: datetime) -> float:
    return (_as_utc_aware(now) - _as_utc_aware(launched_at)).total_seconds() / 60.0


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def resolve_head_price(pair: OrderPairSpec, market: MarketLike) -> float | None:
    """Resolve the head order price from pair configuration and live market state."""
    price, _ = resolve_head_order_prices(pair, market)
    return price


def resolve_head_order_prices(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[float | None, float | None]:
    """Resolve concrete price/stopPx values for a non-market head order.

    The `prix` interval is the hook condition. It is not a limit price. Once the
    hook fires, order placement is anchored to the same market reference source
    that the order type selects.  ``oDelta`` defaults to nominal USD distance;
    when the head carries ``o%`` it is materialised as a percentage of the same
    reference before any exchange command is built.
    """
    code = parse_order_code(pair.head.order_type)
    if code.base_key == "M":
        return None, None
    reference = to_decimal(head_price_reference_price(pair, market)[1])
    if reference <= 0:
        return None, None
    if code.base_key == "L":
        return decimal_to_float(_plain_limit_head_price(pair, reference)), None
    if code.base_key == "S":
        return None, decimal_to_float(_stop_head_price(pair, reference))
    if code.base_key in {"SL", "LT"}:
        price = _trigger_limit_head_price(pair, reference)
        return (
            None if price is None else decimal_to_float(price),
            decimal_to_float(reference),
        )
    if code.base_key in {"SL", "MT", "LT"}:
        return None, decimal_to_float(reference)
    return None, None


def reference_price(side: Side, market: MarketLike) -> float:
    """Return the reference side-aware market price used for relative pricing."""
    if side == Side.BUY:
        return market.best_bid or market.mid_price or 0.0
    return market.best_ask or market.mid_price or 0.0


def _plain_limit_head_price(pair: OrderPairSpec, reference: Decimal) -> Decimal:
    if pair.head.delta is None:
        return reference
    distance = _head_delta_distance(pair, reference)
    if pair.head.side == Side.BUY:
        return reference - distance
    return reference + distance


def _stop_head_price(pair: OrderPairSpec, reference: Decimal) -> Decimal:
    if pair.head.delta is None:
        return reference
    distance = _head_delta_distance(pair, reference)
    if pair.head.side == Side.BUY:
        return reference + distance
    return reference - distance


def _trigger_limit_head_price(pair: OrderPairSpec, reference: Decimal) -> Decimal | None:
    if pair.head.delta is None:
        return None
    distance = _head_delta_distance(pair, reference)
    if pair.head.side == Side.BUY:
        return reference + distance
    return reference - distance


def _head_delta_distance(pair: OrderPairSpec, reference: Decimal) -> Decimal:
    delta = abs(to_decimal(pair.head.delta or 0))
    if pair.head.delta_type.lower() == "o%":
        return reference * delta / Decimal("100")
    return delta


def executable_head_reference_price(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[str, float]:
    """Return the executable public reference for head placement conditions."""
    source = order_price_source(pair.head.order_type)
    if source is not None:
        return source, price_from_source(source, market)
    if pair.head.side == Side.BUY:
        return "ask", _price_or_fallback(market.best_ask, market.mid_price)
    return "bid", _price_or_fallback(market.best_bid, market.mid_price)


def head_price_reference_price(
    pair: OrderPairSpec,
    market: MarketLike,
) -> tuple[str, float]:
    """Return the public reference used to materialise a non-market head price."""
    source = order_price_source(pair.head.order_type)
    if source is not None:
        return source, price_from_source(source, market)
    return "book", reference_price(pair.head.side, market)


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
    code = parse_order_code(order_type)
    if code.base_key in {"S", "SL", "MT", "LT"}:
        return order_price_source(order_type, default="last") or "last"
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


def price_from_source(source: str, market: MarketLike) -> float:
    market_any = market_as_any(market)
    if source == "last":
        return _price_or_fallback(getattr(market_any, "last_price", None), market.mid_price)
    if source == "mark":
        return _price_or_fallback(getattr(market_any, "mark_price", None), market.mid_price)
    if source == "index":
        return _price_or_fallback(getattr(market_any, "index_price", None), market.mid_price)
    if source == "ask":
        return _price_or_fallback(market.best_ask, market.mid_price)
    if source == "bid":
        return _price_or_fallback(market.best_bid, market.mid_price)
    return reference_price(Side.BUY, market)


def market_as_any(market: MarketLike) -> Any:
    return market
