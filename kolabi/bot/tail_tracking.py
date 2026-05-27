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

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from decimal import ROUND_HALF_UP

from kolabi.bot.domain import OrderPairSpec, Side, TailTrailSample, TailTrailState
from kolabi.shared.core.runtime_types import to_decimal

DEFAULT_MAX_SAMPLES = 40


@dataclass(frozen=True)
class TailTrailingConfig:
    update_interval_seconds: int = 6
    unblock_multiplier: Decimal = Decimal("2")
    response_denominator_multiplier: Decimal = Decimal("1.9")
    max_r: Decimal = Decimal("2")
    max_factor: Decimal = Decimal("0.9")
    min_amend_ticks: int = 1
    min_amend_fraction_of_d0: Decimal = Decimal("0")
    alp_left: float = 3.0
    alp_right: float = 1.0
    lam_left: float = 1.0
    lam_right: float = 0.5


@dataclass(frozen=True)
class SkewLomaxCurve:
    alp_left: float = 3.0
    alp_right: float = 1.0
    lam_left: float = 1.0
    lam_right: float = 0.5

    def _left_total(self) -> float:
        a, l = self.alp_left, self.lam_left
        return float(l / a * (1.0 - (1.0 + 1.0 / l) ** (-a)))

    def _right_total(self) -> float:
        a, l = self.alp_right, self.lam_right
        return float(l / a * (1.0 - (1.0 + 1.0 / l) ** (-a)))

    def cdf(self, r: float) -> float:
        r = max(0.0, min(2.0, r))
        z = r - 1.0
        left_total = self._left_total()
        total = left_total + self._right_total()

        if z < 0.0:
            a, l = self.alp_left, self.lam_left
            left_area = l / a * (
                (1.0 + (-z) / l) ** (-a)
                - (1.0 + 1.0 / l) ** (-a)
            )
            return float(left_area / total)

        a, l = self.alp_right, self.lam_right
        right_area = l / a * (1.0 - (1.0 + z / l) ** (-a))
        return float((left_total + right_area) / total)

    def factor(self, r: float, *, max_factor: Decimal) -> Decimal:
        return max_factor * Decimal(str(self.cdf(r)))


DEFAULT_TAIL_TRAILING_CONFIG = TailTrailingConfig()


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
        initial_stop_price=stop,
        last_reference_price=reference,
        last_amended_at=None,
        last_stop_update_at=occurred_at,
    )


def step_tail_trail(
    pair: OrderPairSpec,
    trail: TailTrailState,
    reference_price: Decimal | int | float | str,
    occurred_at: datetime,
    *,
    tick_size: Decimal | int | float | str | None = None,
    config: TailTrailingConfig = DEFAULT_TAIL_TRAILING_CONFIG,
    symbol: str | None = None,
) -> TailTrailState:
    """Advance tail trail by one market tick and amend only on signed improvement."""
    del symbol
    occurred_at = _as_utc_aware(occurred_at)
    reference = to_decimal(reference_price)
    samples = _bounded_samples(
        trail.samples + (TailTrailSample(occurred_at=occurred_at, reference_price=reference),)
    )
    next_state = replace(trail, samples=samples, last_reference_price=reference)

    if trail.baseline_width <= 0:
        return next_state

    if trail.last_amended_at is not None:
        elapsed = occurred_at - _as_utc_aware(trail.last_amended_at)
        if elapsed < timedelta(seconds=config.update_interval_seconds):
            return next_state

    curve = SkewLomaxCurve(
        alp_left=config.alp_left,
        alp_right=config.alp_right,
        lam_left=config.lam_left,
        lam_right=config.lam_right,
    )
    d0 = trail.baseline_width
    previous_ref = trail.last_reference_price
    current_stop = trail.current_stop_price
    tick = _tick_size_or_none(tick_size)
    min_improvement = _min_improvement(d0, tick, config)

    max_lag = config.unblock_multiplier * d0

    if pair.tail.side == Side.SELL:
        d_raw = reference - current_stop
        favorable = previous_ref is None or reference > previous_ref
        if d_raw <= 0 or not favorable:
            return next_state
        if trail.last_amended_at is None and d_raw < config.unblock_multiplier * d0:
            return next_state
        r = _clamp_r(d_raw / (config.response_denominator_multiplier * d0), config.max_r)
        factor = curve.factor(float(r), max_factor=config.max_factor)
        candidate = current_stop + factor * d_raw
        # Hard invariant: do not let stop lag behind reference by more than 2*d0.
        lag_cap_candidate = reference - max_lag
        if candidate < lag_cap_candidate:
            candidate = lag_cap_candidate
        if candidate < current_stop + min_improvement:
            return next_state
        rounded_current = _round_to_tick(current_stop, tick)
        rounded_candidate = _round_to_tick(candidate, tick)
        if rounded_candidate <= rounded_current:
            return next_state
        return replace(
            next_state,
            current_stop_price=rounded_candidate,
            previous_stop_price=current_stop,
            last_amended_at=occurred_at,
            last_stop_update_at=occurred_at,
        )

    d_raw = current_stop - reference
    favorable = previous_ref is None or reference < previous_ref
    if d_raw <= 0 or not favorable:
        return next_state
    if trail.last_amended_at is None and d_raw < config.unblock_multiplier * d0:
        return next_state
    r = _clamp_r(d_raw / (config.response_denominator_multiplier * d0), config.max_r)
    factor = curve.factor(float(r), max_factor=config.max_factor)
    candidate = current_stop - factor * d_raw
    # Hard invariant: do not let stop lag behind reference by more than 2*d0.
    lag_cap_candidate = reference + max_lag
    if candidate > lag_cap_candidate:
        candidate = lag_cap_candidate
    if candidate > current_stop - min_improvement:
        return next_state
    rounded_current = _round_to_tick(current_stop, tick)
    rounded_candidate = _round_to_tick(candidate, tick)
    if rounded_candidate >= rounded_current:
        return next_state
    return replace(
        next_state,
        current_stop_price=rounded_candidate,
        previous_stop_price=current_stop,
        last_amended_at=occurred_at,
        last_stop_update_at=occurred_at,
    )


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


def _bounded_samples(
    samples: tuple[TailTrailSample, ...],
    *,
    max_samples: int = DEFAULT_MAX_SAMPLES,
) -> tuple[TailTrailSample, ...]:
    return samples[-max_samples:]


def _clamp_r(value: Decimal, max_r: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    if value >= max_r:
        return max_r
    return value


def _tick_size_or_none(value: Decimal | int | float | str | None) -> Decimal | None:
    if value is None:
        return None
    tick = to_decimal(value)
    if tick <= 0:
        return None
    return tick


def _min_improvement(
    d0: Decimal,
    tick_size: Decimal | None,
    config: TailTrailingConfig,
) -> Decimal:
    tick_component = (
        Decimal(config.min_amend_ticks) * tick_size if tick_size is not None else Decimal("0")
    )
    fraction_component = config.min_amend_fraction_of_d0 * d0
    return tick_component if tick_component >= fraction_component else fraction_component


def _round_to_tick(value: Decimal, tick_size: Decimal | None) -> Decimal:
    if tick_size is None or tick_size <= 0:
        return value
    return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
