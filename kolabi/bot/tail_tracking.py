"""Pure tail tracking for strategy-managed stop tails.

Purpose: keep the protective tail at its entry width, shorten it on fast
favourable moves, and emit no exchange side effects.
Inputs: immutable pair specification, existing tail trail state, market ticks.
Outputs: immutable tail trail state.
Side effects: none.
Important types: `TailTrailState`, `TailTrailSample`, `OrderPairSpec`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from math import exp

from kolabi.bot.domain import OrderPairSpec, Side, TailTrailSample, TailTrailState
from kolabi.shared.core.runtime_types import to_decimal

DEFAULT_TIME_BIN_SECONDS = 60
DEFAULT_MAX_SAMPLES = 40
DEFAULT_MIN_FLEX = Decimal("0.2")
MAX_PRICE_VARIATION = {
    "XBTUSD": Decimal("2.6"),
    "PI_XBTUSD": Decimal("2.6"),
    "ADAU20": Decimal("2"),
}


def initial_tail_trail(
    pair: OrderPairSpec,
    reference_price: Decimal | int | float | str,
    occurred_at: datetime,
) -> TailTrailState:
    """Create initial tail trail from the active pair tail price grammar."""
    occurred_at = _as_utc_aware(occurred_at)
    reference = to_decimal(reference_price)
    stop = _initial_stop_price(pair, reference)
    sample = TailTrailSample(occurred_at=occurred_at, reference_price=reference)
    return TailTrailState(
        entry_reference_price=reference,
        baseline_width=abs(reference - stop),
        current_stop_price=stop,
        previous_stop_price=stop,
        samples=(sample,),
        last_stop_update_at=occurred_at,
    )


def step_tail_trail(
    pair: OrderPairSpec,
    trail: TailTrailState,
    reference_price: Decimal | int | float | str,
    occurred_at: datetime,
    *,
    symbol: str | None = None,
) -> TailTrailState:
    """Advance tail trail by one market tick and improve protection only."""
    occurred_at = _as_utc_aware(occurred_at)
    reference = to_decimal(reference_price)
    samples = _bounded_samples(
        trail.samples + (TailTrailSample(occurred_at=occurred_at, reference_price=reference),)
    )
    scale = _flex_scale(samples, occurred_at, symbol=symbol)
    signed_width = trail.baseline_width * scale
    candidate = (
        reference - signed_width
        if pair.head.side == Side.BUY
        else reference + signed_width
    )

    if _improves_stop(pair, trail, candidate):
        return replace(
            trail,
            current_stop_price=candidate,
            previous_stop_price=trail.current_stop_price,
            samples=samples,
            last_stop_update_at=occurred_at,
        )
    return replace(trail, samples=samples)


def _initial_stop_price(pair: OrderPairSpec, reference: Decimal) -> Decimal:
    spec = pair.tail_price_spec
    if spec is None:
        raise ValueError(f"Order pair '{pair.name}' needs a tail price specification")
    value = to_decimal(spec)
    tail_type = (pair.tail_price_spec_type or "").lower()
    if "t%" in tail_type or "t%" in pair.amount_type.lower():
        offset = reference * value / Decimal("100")
    elif "td" in tail_type or "td" in pair.amount_type.lower():
        offset = value
    else:
        return value
    return reference - offset if pair.head.side == Side.BUY else reference + offset


def _improves_stop(
    pair: OrderPairSpec,
    trail: TailTrailState,
    candidate: Decimal,
) -> bool:
    if pair.head.side == Side.BUY:
        return (
            candidate > trail.current_stop_price
            and candidate > trail.entry_reference_price
        )
    return (
        candidate < trail.current_stop_price
        and candidate < trail.entry_reference_price
    )


def _bounded_samples(
    samples: tuple[TailTrailSample, ...],
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> tuple[TailTrailSample, ...]:
    return samples[-max_samples:]


def _flex_scale(
    samples: tuple[TailTrailSample, ...],
    occurred_at: datetime,
    *,
    symbol: str | None,
    time_bin_seconds: int = DEFAULT_TIME_BIN_SECONDS,
    min_flex: Decimal = DEFAULT_MIN_FLEX,
) -> Decimal:
    current_var = abs(_current_variation(samples, occurred_at, time_bin_seconds))
    max_var = MAX_PRICE_VARIATION.get(symbol or "", Decimal("2.6"))
    distribution = _neg_exp_distribution(max_var, 100)
    threshold = Decimal(str(-exp(float(current_var + Decimal("1")))))
    rank = Decimal(sum(1 for value in distribution if value < threshold)) / Decimal(len(distribution))
    return (Decimal("1") - rank) * min_flex + rank


def _current_variation(
    samples: tuple[TailTrailSample, ...],
    occurred_at: datetime,
    time_bin_seconds: int,
) -> Decimal:
    occurred_at = _as_utc_aware(occurred_at)
    current_start = occurred_at - timedelta(seconds=time_bin_seconds)
    previous_start = occurred_at - timedelta(seconds=2 * time_bin_seconds)
    current = [
        sample.reference_price
        for sample in samples
        if _as_utc_aware(sample.occurred_at) > current_start
    ]
    previous = [
        sample.reference_price
        for sample in samples
        if previous_start < _as_utc_aware(sample.occurred_at) <= current_start
    ]
    if not current or not previous:
        return Decimal("0")
    current_mean = sum(current) / Decimal(len(current))
    previous_mean = sum(previous) / Decimal(len(previous))
    if previous_mean == 0:
        return Decimal("0")
    return (current_mean - previous_mean) / previous_mean * Decimal("100")


def _neg_exp_distribution(max_var: Decimal, count: int) -> tuple[Decimal, ...]:
    if count <= 1:
        return (Decimal(str(-exp(float(max_var)))),)
    step = (max_var - Decimal("1")) / Decimal(count - 1)
    return tuple(
        Decimal(str(-exp(float(Decimal("1") + step * Decimal(index)))))
        for index in range(count)
    )


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
