from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


NumberPair = tuple[float, float]


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class HeadState(StrEnum):
    LATENT = "latent"
    HOOKED = "hooked"
    SUBMITTED = "submitted"
    NEW = "new"
    LIVING = "living"
    FAILED = "failed"
    CLOSED = "closed"


class TailState(StrEnum):
    LATENT = "latent"
    HOOKED = "hooked"
    SUBMITTED = "submitted"
    FLAPPING = "flapping"
    FLYING = "flying"


# Compatibility aliases used by the current pair-cycle runtime.
OrderState = HeadState
TailMode = TailState


class OrderReason(StrEnum):
    LIMIT_ORDER_FROM_STOP = "limit_order_from_stop"
    NEW_PLACED_ORDER_BY_USER = "new_placed_order_by_user"
    CANCELLED_BY_ADMIN = "cancelled_by_admin"
    CANCELLED_BY_USER = "cancelled_by_user"
    CONTRACT_EXPIRED = "contract_expired"
    DEAD_MAN_SWITCH = "dead_man_switch"
    LIQUIDATION = "liquidation"
    MARKET_INACTIVE = "market_inactive"
    NOT_ENOUGH_MARGIN = "not_enough_margin"
    ORDER_FOR_EDIT_NOT_FOUND = "order_for_edit_not_found"
    PARTIAL_FILL = "partial_fill"
    FULL_FILL = "full_fill"
    IOC_WOULD_NOT_EXECUTE = "ioc_order_failed_because_it_would_not_be_executed"
    POST_ONLY_WOULD_FILL = "post_order_failed_because_it_would_filled"
    STOP_ORDER_TRIGGERED = "stop_order_triggered"
    WOULD_EXECUTE_SELF = "would_execute_self"
    WOULD_NOT_REDUCE_POSITION = "would_not_reduce_position"
    UNKNOWN = "unknown"


PLAYED_REASONS = frozenset(
    {
        OrderReason.FULL_FILL,
        OrderReason.PARTIAL_FILL,
        OrderReason.IOC_WOULD_NOT_EXECUTE,
        OrderReason.POST_ONLY_WOULD_FILL,
        OrderReason.STOP_ORDER_TRIGGERED,
        OrderReason.WOULD_EXECUTE_SELF,
        OrderReason.WOULD_NOT_REDUCE_POSITION,
    }
)


@dataclass(frozen=True)
class TimeWindow:
    start_minutes: float
    end_minutes: float


@dataclass(frozen=True)
class HeadSpec:
    side: Side
    order_type: str
    price_interval: NumberPair
    quantity: int | None
    delta: float | None


@dataclass(frozen=True)
class TailSpec:
    order_type: str
    price: float | None
    delta: float | None


@dataclass(frozen=True)
class OrderPairSpec:
    name: str
    window: TimeWindow
    attempts: int
    pause_minutes: float | None
    timeout_minutes: int | None
    head: HeadSpec
    tail: TailSpec
    amount_type: str
    hook: str | None = None

    @property
    def tps_run(self) -> NumberPair:
        return (self.window.start_minutes, self.window.end_minutes)

    @property
    def essais(self) -> int:
        return self.attempts

    @property
    def dr_pause(self) -> float | None:
        return self.pause_minutes

    @property
    def timeout(self) -> int | None:
        return self.timeout_minutes

    @property
    def side(self) -> str:
        return self.head.side.value

    @property
    def prix(self) -> NumberPair:
        return self.head.price_interval

    @property
    def q(self) -> int | None:
        return self.head.quantity

    @property
    def tp(self) -> float | None:
        return self.tail.price

    @property
    def atype(self) -> str:
        return self.amount_type

    @property
    def oType(self) -> str:
        return self.head.order_type

    @property
    def oDelta(self) -> float | None:
        return self.head.delta

    @property
    def tDelta(self) -> float | None:
        return self.tail.delta

    @property
    def tType(self) -> str:
        return self.tail.order_type


@dataclass(frozen=True)
class OrderIdentity:
    pair_name: str
    role: str
    client_order_id: str | None = None
    exchange_order_id: str | None = None


@dataclass(frozen=True)
class ConfirmedOrder:
    identity: OrderIdentity
    state: HeadState
    reason: OrderReason
    filled_quantity: float
    total_quantity: float

    @property
    def is_played(self) -> bool:
        return self.reason in PLAYED_REASONS or self.filled_quantity > 0

    @property
    def is_terminal(self) -> bool:
        return self.state in {OrderState.FAILED, OrderState.CLOSED}


@dataclass(frozen=True)
class PairCycleState:
    pair: OrderPairSpec
    head_state: HeadState = HeadState.LATENT
    tail_mode: TailState | None = None
    head_identity: OrderIdentity | None = None
    tail_identity: OrderIdentity | None = None
    played_quantity: float = 0.0


@dataclass(frozen=True)
class PairCycleEvent:
    pair_name: str
    state: PairCycleState
    message: str


class ExchangeOrderAck(Protocol):
    order_id: str
    status: str
    price: float | None
    orig_qty: float | None
    executed_qty: float | None
    side: str | None


def normalize_side(raw: str) -> Side:
    value = raw.strip().lower()
    if value == "buy":
        return Side.BUY
    if value == "sell":
        return Side.SELL
    raise ValueError(f"Unsupported side '{raw}'")


def normalize_reason(raw: str | None) -> OrderReason:
    if not raw:
        return OrderReason.UNKNOWN
    value = raw.strip().lower()
    for reason in OrderReason:
        if reason.value == value:
            return reason
    return OrderReason.UNKNOWN


def classify_confirmed_state(*, is_played: bool, is_canceled: bool) -> HeadState:
    if is_played and is_canceled:
        return HeadState.CLOSED
    if is_played and not is_canceled:
        return HeadState.LIVING
    if not is_played and is_canceled:
        return HeadState.FAILED
    return HeadState.NEW


def can_hook_tail(head: ConfirmedOrder) -> bool:
    return head.is_played and head.state != HeadState.FAILED


def tail_mode_for_head(head: ConfirmedOrder) -> TailState | None:
    if not can_hook_tail(head):
        return None
    if head.state == HeadState.LIVING:
        return TailState.FLAPPING
    if head.state == HeadState.CLOSED:
        return TailState.FLYING
    return TailState.HOOKED
