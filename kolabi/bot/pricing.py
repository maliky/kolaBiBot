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

from kolabi.bot.domain import OrderPairSpec, Side
from kolabi.shared.core.runtime_types import decimal_to_float, to_decimal
from kolabi.shared.runtime_state import PublicMarketState


def pair_window_is_active(
    pair: OrderPairSpec,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    """Return true when current time is inside the pair launch-relative window."""
    elapsed_minutes = (now - launched_at).total_seconds() / 60.0
    return pair.window.start_minutes <= elapsed_minutes <= pair.window.end_minutes


def resolve_head_price(pair: OrderPairSpec, market: PublicMarketState) -> float | None:
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


def reference_price(side: Side, market: PublicMarketState) -> float:
    """Return the reference side-aware market price used for relative pricing."""
    if side == Side.BUY:
        return market.best_bid or market.mid_price or 0.0
    return market.best_ask or market.mid_price or 0.0
