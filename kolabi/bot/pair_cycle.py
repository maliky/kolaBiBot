"""Pair-cycle runtime reducer and execution bridge.

Purpose: run one head/tail pair cycle against runtime state and exchange
gateway, with pure transition decisions in `step_pair`.
Inputs: `OrderPairSpec`, runtime market state, exchange acknowledgements,
runtime events.
Outputs: `PairCycleResult`, transition commands, and normalized broker replies.
Side effects: exchange submission in runner methods and threaded IO bridging.
Important types: `PairCycleState`, `RuntimeEvent`, `RuntimeCommand`,
`OrderAck`, `ExecutionOutcome`.
Role: interpreter shell plus pure reducer core.
Transitional: yes, bridges legacy vocabulary while moving to typed commands.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, cast

from kolabi.bot.domain import (
    ConfirmedOrder,
    ExecutionOutcome,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    OrderReason,
    PairCycleEvent,
    PairCycleState,
    Side,
    TailState,
    classify_confirmed_state,
    normalize_reason,
)
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    BrokerReply,
    OrderDict,
    OrderQty,
    OrderRole,
    PriceOffset,
    RuntimeCommand,
    RuntimeCommandKind,
    RuntimeEvent,
    RuntimeEventKind,
    StopPrice,
    Symbol,
    decimal_to_float,
    to_decimal,
)
from kolabi.shared.runtime_state import KrakenRuntimeStateClient, PublicMarketState

PAIR_EVENT_HEAD_HOOKED = "head_hooked"
PAIR_EVENT_HEAD_SUBMITTED = "head_submitted"
PAIR_EVENT_HEAD_PLAYED = "head_played"
PAIR_EVENT_HEAD_PARTIAL_FILL = "head_partial_fill"
PAIR_EVENT_HEAD_FAILED = "head_failed"
PAIR_EVENT_HEAD_CANCELED = "head_canceled"
PAIR_EVENT_HEAD_CLOSED = "head_closed"
PAIR_EVENT_HEAD_ACKNOWLEDGED = "head_acknowledged"


class ExchangeGateway(Protocol):
    def place_order(
        self,
        side: str,
        orderQty: float,
        price: float | None = None,
        stopPx: float | None = None,
        type_: str = "LIMIT",
        **params: object,
    ) -> OrderAck: ...

    def amend_order(self, order_id: str, **params: float) -> OrderAck: ...

    def instrument_rules(self, symbol: str | None = None) -> dict[str, object]: ...


@dataclass(frozen=True)
class PairCycleResult:
    state: PairCycleState
    events: tuple[PairCycleEvent, ...]


class PairCycleRunner:
    """Runs one typed pair/head/tail cycle against DB state and an exchange."""

    def __init__(
        self,
        *,
        exchange: ExchangeGateway,
        runtime_state: KrakenRuntimeStateClient | None,
        symbol: str,
        dry_run: bool = False,
    ) -> None:
        self.exchange = exchange
        self.runtime_state = runtime_state
        self.symbol = symbol
        self.dry_run = dry_run

    async def run_pairs_once(
        self,
        pairs: list[OrderPairSpec],
    ) -> list[PairCycleResult]:
        """Evaluate each pair once, concurrently at the IO boundary."""
        return await asyncio.gather(*(self.run_pair_once(pair) for pair in pairs))

    async def run_pair_once(self, pair: OrderPairSpec) -> PairCycleResult:
        """Advance one order pair by at most one automatic step."""
        state = PairCycleState(pair=pair)
        events: list[PairCycleEvent] = []
        market = self._market_state()
        if not pair_window_is_active(pair):
            return self._result(state, events, "pair outside time window")
        if market is None or not market.ready:
            return self._result(state, events, "public market state is not ready")

        symbol = Symbol(self.symbol)
        hooked, _ = step_pair(
            state,
            RuntimeEvent(
                kind=RuntimeEventKind.ORDER_REQUESTED,
                at=datetime.now(timezone.utc),
                symbol=symbol,
                note=PAIR_EVENT_HEAD_HOOKED,
            ),
        )
        events.append(PairCycleEvent(pair.name, hooked, "head hooked"))
        if self.dry_run:
            return self._result(hooked, events, "dry run stopped before submission")

        client_order_id = head_client_order_id(pair)
        submission_ack = await asyncio.to_thread(
            self._submit_head,
            pair,
            market,
            client_order_id,
        )
        submitted, _ = step_pair(
            hooked,
            RuntimeEvent(
                kind=RuntimeEventKind.ORDER_ACK,
                at=datetime.now(timezone.utc),
                symbol=symbol,
                order=head_order_dict(pair, client_order_id=client_order_id),
                reply=broker_reply_from_ack(submission_ack, client_order_id=client_order_id),
                note=PAIR_EVENT_HEAD_SUBMITTED,
            ),
        )
        events.append(PairCycleEvent(pair.name, submitted, "head submitted"))

        confirmed_head = confirmed_from_ack(pair, submission_ack)
        advanced, commands = step_pair(
            submitted,
            runtime_event_from_confirmed_head(
                pair,
                confirmed_head,
                symbol=symbol,
            ),
        )
        if advanced.tail_mode in {None, TailState.LATENT}:
            return self._result(advanced, events, "tail not eligible yet")
        del commands
        assert advanced.tail_mode is not None
        events.append(
            PairCycleEvent(pair.name, advanced, f"tail mode {advanced.tail_mode.value}")
        )
        return PairCycleResult(advanced, tuple(events))

    def _market_state(self) -> PublicMarketState | None:
        if self.runtime_state is None:
            return None
        return self.runtime_state.fetch_market_state(self.symbol)

    def _submit_head(
        self,
        pair: OrderPairSpec,
        market: PublicMarketState,
        client_order_id: str,
    ) -> OrderAck:
        price = resolve_head_price(pair, market)
        quantity = resolve_quantity(pair)
        return self.exchange.place_order(
            side=pair.head.side.value,
            orderQty=quantity,
            price=price,
            type_=pair.head.order_type,
            clOrdID=client_order_id,
        )

    @staticmethod
    def _result(
        state: PairCycleState,
        events: list[PairCycleEvent],
        message: str,
    ) -> PairCycleResult:
        events.append(PairCycleEvent(state.pair.name, state, message))
        return PairCycleResult(state=state, events=tuple(events))


def pair_window_is_active(pair: OrderPairSpec) -> bool:
    """Return true when the relative launch window can run now.

    TSV windows are expressed relative to launch time. During a one-shot cycle,
    any window containing minute zero is active.
    """
    return pair.window.start_minutes <= 0 <= pair.window.end_minutes


def step_pair(
    state: PairCycleState,
    event: RuntimeEvent,
) -> tuple[PairCycleState, tuple[RuntimeCommand, ...]]:
    """Pure reducer for one pair/head/tail transition."""
    if event.note == PAIR_EVENT_HEAD_HOOKED:
        next_state = replace(state, head_state=HeadState.HOOKED)
        return next_state, (
            RuntimeCommand(
                kind=RuntimeCommandKind.PLACE,
                symbol=event.symbol,
                order=head_order_dict(next_state.pair),
                reason=OrderRole.PRIMARY.value,
            ),
        )
    if event.note == PAIR_EVENT_HEAD_SUBMITTED:
        next_state = replace(
            state,
            head_state=HeadState.SUBMITTED,
            head_identity=head_identity_from_event(state, event),
        )
        return next_state, ()
    if event.note == PAIR_EVENT_HEAD_FAILED:
        return (
            replace(
                state,
                head_state=HeadState.FAILED,
                head_identity=head_identity_from_event(state, event),
                tail_mode=TailState.LATENT,
                played_quantity=played_quantity_from_event(state, event),
            ),
            (),
        )
    if event.note == PAIR_EVENT_HEAD_CANCELED:
        played_quantity = played_quantity_from_event(state, event)
        if played_quantity > 0:
            next_state = replace(
                state,
                head_state=HeadState.CLOSED,
                head_identity=head_identity_from_event(state, event),
                tail_mode=TailState.FLYING,
                played_quantity=played_quantity,
            )
            return next_state, (tail_command(next_state, event, RuntimeCommandKind.PLACE),)
        next_state = replace(
            state,
            head_state=HeadState.FAILED,
            head_identity=head_identity_from_event(state, event),
            tail_mode=TailState.LATENT,
            played_quantity=0.0,
        )
        return next_state, ()
    if event.note == PAIR_EVENT_HEAD_PLAYED:
        played_quantity = played_quantity_from_event(state, event)
        next_state = replace(
            state,
            head_state=HeadState.LIVING,
            head_identity=head_identity_from_event(state, event),
            tail_mode=TailState.HOOKED,
            played_quantity=played_quantity,
        )
        return next_state, (tail_command(next_state, event, RuntimeCommandKind.PLACE),)
    if event.note == PAIR_EVENT_HEAD_PARTIAL_FILL:
        played_quantity = played_quantity_from_event(state, event)
        next_state = replace(
            state,
            head_state=HeadState.LIVING,
            head_identity=head_identity_from_event(state, event),
            tail_mode=TailState.FLAPPING,
            played_quantity=played_quantity,
        )
        command_kind = (
            RuntimeCommandKind.AMEND
            if state.tail_identity is not None
            else RuntimeCommandKind.PLACE
        )
        return next_state, (tail_command(next_state, event, command_kind),)
    if event.note == PAIR_EVENT_HEAD_CLOSED:
        played_quantity = played_quantity_from_event(state, event)
        next_state = replace(
            state,
            head_state=HeadState.CLOSED,
            head_identity=head_identity_from_event(state, event),
            tail_mode=TailState.FLYING,
            played_quantity=played_quantity,
        )
        return next_state, (tail_command(next_state, event, RuntimeCommandKind.PLACE),)
    return state, ()


def resolve_quantity(pair: OrderPairSpec) -> float:
    quantity = pair.head.quantity
    if quantity is None or quantity <= 0:
        raise ValueError(f"Order pair '{pair.name}' needs a positive head quantity")
    return decimal_to_float(quantity)


def resolve_head_price(pair: OrderPairSpec, market: PublicMarketState) -> float | None:
    order_type = pair.head.order_type.replace("_", "").replace("-", "").lower()
    if order_type in {"m", "market"}:
        return None
    reference = to_decimal(reference_price(pair.head.side, market))
    lower, upper = pair.head.price_interval
    if "pA" in pair.amount_type:
        return decimal_to_float(lower if pair.head.side == Side.BUY else upper)
    if "p%" in pair.amount_type:
        offset = to_decimal(lower if pair.head.side == Side.BUY else upper)
        return decimal_to_float(reference * (Decimal("1") + offset / Decimal("100")))
    offset = to_decimal(lower if pair.head.side == Side.BUY else upper)
    return decimal_to_float(reference + offset)


def reference_price(side: Side, market: PublicMarketState) -> float:
    if side == Side.BUY:
        return market.best_bid or market.mid_price or 0.0
    return market.best_ask or market.mid_price or 0.0


def head_client_order_id(pair: OrderPairSpec) -> str:
    safe_name = "".join(ch for ch in pair.name if ch.isalnum() or ch in {"_", "-"})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"kolabi-{safe_name}-head-{stamp}"[:64]


def opposite_side(side: Side) -> Side:
    if side == Side.BUY:
        return Side.SELL
    return Side.BUY


def head_order_dict(pair: OrderPairSpec, *, client_order_id: str | None = None) -> OrderDict:
    order: OrderDict = {
        "side": pair.head.side.value,
        "ordType": pair.head.order_type,
    }
    if pair.head.quantity is not None:
        order["orderQty"] = cast(OrderQty, to_decimal(pair.head.quantity))
    if client_order_id is not None:
        order["clOrdID"] = client_order_id
    return order


def tail_order_dict(pair: OrderPairSpec) -> OrderDict:
    order: OrderDict = {
        "side": opposite_side(pair.head.side).value,
        "ordType": pair.tail.order_type,
    }
    if pair.head.quantity is not None:
        order["orderQty"] = cast(OrderQty, to_decimal(pair.head.quantity))
    if pair.tail.price is not None:
        order["stopPx"] = cast(StopPrice, to_decimal(pair.tail.price))
    if pair.tail.delta is not None:
        order["oDelta"] = cast(PriceOffset, to_decimal(pair.tail.delta))
    return order


def tail_command(
    state: PairCycleState,
    event: RuntimeEvent,
    kind: RuntimeCommandKind,
) -> RuntimeCommand:
    return RuntimeCommand(
        kind=kind,
        symbol=event.symbol,
        order=tail_order_dict(state.pair),
        reason=OrderRole.TAIL.value,
    )


def broker_reply_from_ack(
    ack: OrderAck,
    *,
    client_order_id: str | None = None,
) -> BrokerReply:
    reply: BrokerReply = {
        "orderID": ack.order_id,
        "ordStatus": ack.status,
    }
    if client_order_id is not None:
        reply["clOrdID"] = client_order_id
    if ack.side is not None:
        reply["side"] = ack.side
    if ack.price is not None:
        reply["price"] = ack.price
    if ack.executed_qty is not None:
        reply["cumQty"] = ack.executed_qty  # type: ignore[typeddict-unknown-key]
    if ack.orig_qty is not None:
        reply["orderQty"] = ack.orig_qty  # type: ignore[typeddict-unknown-key]
    return reply


def head_identity_from_event(
    state: PairCycleState,
    event: RuntimeEvent,
) -> OrderIdentity | None:
    reply = event.reply or {}
    order = event.order or {}
    order_id = reply.get("orderID")
    client_order_id = reply.get("clOrdID") or order.get("clOrdID")
    if order_id is None and client_order_id is None:
        return state.head_identity
    return OrderIdentity(
        pair_name=state.pair.name,
        role="head",
        client_order_id=str(client_order_id) if client_order_id is not None else None,
        exchange_order_id=str(order_id) if order_id is not None else None,
    )


def played_quantity_from_event(
    state: PairCycleState,
    event: RuntimeEvent,
) -> float:
    reply = event.reply or {}
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = reply.get(key)  # type: ignore[arg-type]
        if isinstance(value, (int, float)):
            return float(value)
    return state.played_quantity


def runtime_event_from_confirmed_head(
    pair: OrderPairSpec,
    head: ConfirmedOrder,
    *,
    symbol: Symbol,
) -> RuntimeEvent:
    note = PAIR_EVENT_HEAD_ACKNOWLEDGED
    if head.state == HeadState.FAILED:
        note = PAIR_EVENT_HEAD_FAILED
    elif head.state == HeadState.CLOSED and head.filled_quantity > 0:
        note = PAIR_EVENT_HEAD_CLOSED
    elif head.state == HeadState.CLOSED:
        note = PAIR_EVENT_HEAD_CANCELED
    elif head.reason == OrderReason.PARTIAL_FILL:
        note = PAIR_EVENT_HEAD_PARTIAL_FILL
    elif head.is_played:
        note = PAIR_EVENT_HEAD_PLAYED
    return RuntimeEvent(
        kind=RuntimeEventKind.ORDER_VALIDATED,
        at=datetime.now(timezone.utc),
        symbol=symbol,
        reply={
            "orderID": head.identity.exchange_order_id or "",
            "clOrdID": head.identity.client_order_id or "",
            "ordStatus": head.state.value,
            "execType": head.reason.value,
            "cumQty": head.filled_quantity,  # type: ignore[typeddict-unknown-key]
            "orderQty": head.total_quantity,  # type: ignore[typeddict-unknown-key]
        },
        note=note,
    )


def confirmed_from_ack(pair: OrderPairSpec, ack: OrderAck) -> ConfirmedOrder:
    reason = reason_from_status(ack.status)
    status = ack.status.strip().lower()
    played = reason in {
        OrderReason.FULL_FILL,
        OrderReason.PARTIAL_FILL,
        OrderReason.STOP_ORDER_TRIGGERED,
    } or bool(ack.executed_qty and ack.executed_qty > 0)
    canceled = status in {"canceled", "cancelled", "filled"}
    if canceled and played:
        outcome = ExecutionOutcome.CANCELED_PLAYED
    elif canceled and not played:
        outcome = ExecutionOutcome.CANCELED_UNPLAYED
    elif played:
        outcome = ExecutionOutcome.PLAYED
    else:
        outcome = ExecutionOutcome.NEW
    state = classify_confirmed_state(outcome)
    return ConfirmedOrder(
        identity=OrderIdentity(
            pair_name=pair.name,
            role="head",
            client_order_id=None,
            exchange_order_id=ack.order_id,
        ),
        state=state,
        reason=reason,
        filled_quantity=decimal_to_float(ack.executed_qty or 0.0),
        total_quantity=decimal_to_float(ack.orig_qty or pair.head.quantity or 0.0),
    )


def reason_from_status(status: str) -> OrderReason:
    normalized = status.replace(" ", "_").replace("-", "_").lower()
    if normalized in {"partiallyfilled", "partial_fill"}:
        return OrderReason.PARTIAL_FILL
    if normalized in {"filled", "full_fill"}:
        return OrderReason.FULL_FILL
    if normalized in {"canceled", "cancelled"}:
        return OrderReason.CANCELLED_BY_USER
    if normalized in {"new", "open"}:
        return OrderReason.NEW_PLACED_ORDER_BY_USER
    return normalize_reason(normalized)
