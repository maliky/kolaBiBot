"""Orange : Pure event ingestion normalizer and translator.

Purpose: translate raw execution, confirmation, and synthetic runtime facts
into reducer-ready `EggMove` values without supervisor side effects.
Inputs: typed acknowledgements, normalized private order facts, and simulated
execution facts.
Outputs: typed `EggMove` values or typed unresolved results.
Side effects: none.
Important types: `EggMove`, `OrderAck`, `ConfirmedOrder`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

from kolabi.bot.domain import OrderPairSpec
from kolabi.bot.pricing import pair_window_is_open
from kolabi.bot.domain import (
    ConfirmedOrder,
    EggMove,
    EggMoveKind,
    HeadState,
    OrderIdentity,
    OrderReason,
    classify_confirmed_move,
)
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    BrokerReply,
    PrivateOrderRecord,
    to_decimal,
)


@dataclass(frozen=True)
class MarketSnapshotFact:
    symbol: str
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    occurred_at: datetime


@dataclass(frozen=True)
class PrivateOrderFact:
    pair_name: str | None
    symbol: str
    order_id: str | None
    client_order_id: str | None
    status: str
    reason: str | None
    filled_quantity: Decimal
    total_quantity: Decimal
    occurred_at: datetime


def head_hooked_from_market_snapshot(
    *,
    pair: OrderPairSpec,
    launched_at: datetime,
    snapshot: MarketSnapshotFact,
) -> EggMove | None:
    if not pair_window_is_open(pair, launched_at=launched_at, now=snapshot.occurred_at):
        return None
    if snapshot.mid_price is None and snapshot.best_bid is None and snapshot.best_ask is None:
        return None
    return head_hooked_event(
        pair_name=pair.name,
        symbol=snapshot.symbol,
        occurred_at=snapshot.occurred_at,
    )


def head_hooked_event(
    *,
    pair_name: str,
    symbol: str,
    occurred_at: datetime | None = None,
) -> EggMove:
    return EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        symbol=symbol,
        pair_name=pair_name,
    )


def head_submitted_from_ack(
    *,
    pair_name: str,
    symbol: str,
    ack: OrderAck,
    client_order_id: str | None,
    occurred_at: datetime | None = None,
) -> EggMove:
    reply: BrokerReply = {
        "orderID": str(ack.order_id),
        "ordStatus": ack.status,
    }
    if client_order_id is not None:
        reply["clOrdID"] = client_order_id
    if ack.orig_qty is not None:
        reply["orderQty"] = float(to_decimal(ack.orig_qty))
    if ack.executed_qty is not None:
        reply["cumQty"] = float(to_decimal(ack.executed_qty))
    if ack.side is not None:
        reply["side"] = ack.side
    if ack.price is not None:
        reply["price"] = ack.price
    return EggMove(
        kind=EggMoveKind.HEAD_SUBMITTED,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        symbol=symbol,
        pair_name=pair_name,
        reply=reply,
        is_private=False,
    )


def private_order_fact_from_mapping(
    payload: Mapping[str, object],
    *,
    pair_name: str | None,
    symbol: str,
    occurred_at: datetime | None = None,
) -> PrivateOrderFact:
    return PrivateOrderFact(
        pair_name=pair_name,
        symbol=symbol,
        order_id=_string_or_none(payload.get("orderID")),
        client_order_id=_string_or_none(payload.get("clOrdID")),
        status=str(payload.get("ordStatus") or payload.get("status") or ""),
        reason=_string_or_none(payload.get("execType")) or _string_or_none(payload.get("reason")),
        filled_quantity=_decimal_or_zero(
            payload.get("cumQty")
            or payload.get("executedQty")
            or payload.get("filledQty")
            or payload.get("filled_quantity")
        ),
        total_quantity=_decimal_or_zero(payload.get("orderQty") or payload.get("quantity")),
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )


def private_order_fact_from_record(
    record: PrivateOrderRecord,
    *,
    pair_name: str | None = None,
) -> PrivateOrderFact:
    occurred_at = _datetime_from_iso(record.source_timestamp) or _datetime_from_iso(
        record.local_timestamp
    ) or datetime.now(timezone.utc)
    return PrivateOrderFact(
        pair_name=pair_name,
        symbol=record.symbol,
        order_id=record.exchange_order_id,
        client_order_id=record.client_order_id,
        status=record.status,
        reason=None,
        filled_quantity=_decimal_or_zero(record.filled_quantity),
        total_quantity=_decimal_or_zero(record.quantity),
        occurred_at=occurred_at,
    )


def confirmed_head_from_private_fact(fact: PrivateOrderFact) -> ConfirmedOrder:
    reason = reason_from_status_or_reason(fact.status, fact.reason)
    state = state_from_status_or_reason(fact.status, reason)
    return ConfirmedOrder(
        identity=OrderIdentity(
            pair_name=fact.pair_name or "",
            role="head",
            client_order_id=fact.client_order_id,
            exchange_order_id=fact.order_id,
        ),
        state=state,
        reason=reason,
        filled_quantity=fact.filled_quantity,
        total_quantity=fact.total_quantity,
    )


def head_move_from_private_fact(fact: PrivateOrderFact) -> EggMove:
    head = confirmed_head_from_private_fact(fact)
    return EggMove(
        kind=classify_confirmed_move(head),
        occurred_at=fact.occurred_at,
        symbol=fact.symbol,
        pair_name=fact.pair_name,
        reply={
            "orderID": head.identity.exchange_order_id or "",
            "clOrdID": head.identity.client_order_id or "",
            "ordStatus": head.state.value,
            "execType": head.reason.value,
            "cumQty": float(head.filled_quantity),
            "orderQty": float(head.total_quantity),
        },
        is_private=True,
    )


def simulated_private_fill_from_submission(
    submitted: EggMove,
    *,
    played_quantity: Decimal | int | float | str,
    closed: bool = True,
) -> EggMove:
    reply = dict(submitted.reply or {})
    filled = to_decimal(played_quantity)
    reply["cumQty"] = float(filled)
    total_qty = reply.get("orderQty", played_quantity if filled > 0 else 0)
    reply["orderQty"] = float(_decimal_or_zero(total_qty))
    if closed:
        reply["ordStatus"] = HeadState.CLOSED.value
        reply["execType"] = OrderReason.FULL_FILL.value if filled > 0 else OrderReason.CANCELLED_BY_USER.value
    else:
        reply["ordStatus"] = HeadState.LIVING.value
        reply["execType"] = OrderReason.PARTIAL_FILL.value
    fact = private_order_fact_from_mapping(
        reply,
        pair_name=submitted.pair_name,
        symbol=submitted.symbol,
        occurred_at=submitted.occurred_at,
    )
    return head_move_from_private_fact(fact)


def reason_from_status_or_reason(status: str, reason: str | None) -> OrderReason:
    if reason:
        normalized_reason = reason.strip().lower()
        for candidate in OrderReason:
            if candidate.value == normalized_reason:
                return candidate
    normalized = status.replace(" ", "_").replace("-", "_").lower()
    if normalized in {"partiallyfilled", "partially_filled", "partial_fill", HeadState.LIVING.value}:
        return OrderReason.PARTIAL_FILL
    if normalized in {"filled", "full_fill", "fully_filled", HeadState.CLOSED.value}:
        return OrderReason.FULL_FILL
    if normalized in {"canceled", "cancelled", HeadState.FAILED.value}:
        return OrderReason.CANCELLED_BY_USER
    if normalized in {"new", "open", HeadState.NEW.value}:
        return OrderReason.NEW_PLACED_ORDER_BY_USER
    return OrderReason.UNKNOWN


def state_from_status_or_reason(status: str, reason: OrderReason) -> HeadState:
    normalized = status.replace(" ", "_").replace("-", "_").lower()
    if normalized in {"new", "open"}:
        return HeadState.NEW
    if normalized in {"partiallyfilled", "partially_filled", "partial_fill", HeadState.LIVING.value}:
        return HeadState.LIVING
    if normalized in {"filled", "full_fill", "fully_filled", HeadState.CLOSED.value}:
        return HeadState.CLOSED
    if normalized in {"canceled", "cancelled", HeadState.FAILED.value}:
        return HeadState.FAILED
    if reason in {
        OrderReason.FULL_FILL,
        OrderReason.IOC_WOULD_NOT_EXECUTE,
        OrderReason.POST_ONLY_WOULD_FILL,
        OrderReason.STOP_ORDER_TRIGGERED,
        OrderReason.WOULD_EXECUTE_SELF,
        OrderReason.WOULD_NOT_REDUCE_POSITION,
    }:
        return HeadState.CLOSED
    if reason == OrderReason.PARTIAL_FILL:
        return HeadState.LIVING
    if reason in {
        OrderReason.CANCELLED_BY_ADMIN,
        OrderReason.CANCELLED_BY_USER,
        OrderReason.CONTRACT_EXPIRED,
        OrderReason.DEAD_MAN_SWITCH,
        OrderReason.LIQUIDATION,
        OrderReason.MARKET_INACTIVE,
        OrderReason.NOT_ENOUGH_MARGIN,
        OrderReason.ORDER_FOR_EDIT_NOT_FOUND,
    }:
        return HeadState.FAILED
    return HeadState.NEW


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _decimal_or_zero(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, (int, float, Decimal, str)):
        return max(to_decimal(value), Decimal("0"))
    return Decimal("0")


def _datetime_from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)
