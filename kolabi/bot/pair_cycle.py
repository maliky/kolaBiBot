"""Pair-cycle reducer and runner bridge for the active bot runtime.

Purpose: evaluate one head/tail pair lifecycle through typed reducer moves and
emit typed command intents, while keeping exchange IO in a thin runner shell.
Inputs: `OrderPairSpec`, `StrategyState`, `EggMove`, market snapshots, and
exchange acknowledgements.
Outputs: `PairCycleResult`, updated strategy memory, and command intents.
Side effects: exchange submission in runner methods and async/thread IO bridging.
Important types: `PairCycleState`, `StrategyState`, `EggMove`, `PairIntent`,
`RuntimeCommand`, `OrderAck`.
Role: interpreter shell plus pure reducer core.
Transitional: yes, still bridges legacy payloads at boundary helpers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, cast

from kolabi.bot.domain import (
    classify_confirmed_move,
    ConfirmedOrder,
    EggMove,
    EggMoveKind,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    PairIntent,
    PairIntentKind,
    OrderReason,
    PairCycleEvent,
    PairCycleState,
    Side,
    StrategyState,
    TailMode,
    TailState,
    normalize_reason,
)
from kolabi.bot.ids import head_client_order_id
from kolabi.bot.janus import plan_runtime_commands
from kolabi.bot.order_building import head_order_dict
from kolabi.bot.pricing import pair_window_is_open, resolve_head_price
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import BrokerReply, Symbol, to_decimal
from kolabi.shared.runtime_state import KrakenRuntimeStateClient, PublicMarketState


class ExchangeGateway(Protocol):
    """Exchange write boundary used by the runner shell."""

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
    """Runner result payload for one pair pass."""

    state: PairCycleState
    events: tuple[PairCycleEvent, ...]


class PairCycleRunner:
    """Run one typed pair cycle against DB state and an exchange boundary."""

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
        self.strategy_state: StrategyState | None = None

    async def run_pairs_once(self, pairs: list[OrderPairSpec]) -> list[PairCycleResult]:
        now = datetime.now(timezone.utc)
        return await asyncio.gather(*(self.run_pair_once(pair, now=now) for pair in pairs))

    async def run_pair_once(
        self,
        pair: OrderPairSpec,
        *,
        now: datetime | None = None,
    ) -> PairCycleResult:
        current_time = now or datetime.now(timezone.utc)
        state = self._state_for_pair(pair, current_time)
        events: list[PairCycleEvent] = []
        market = self._market_state()
        assert self.strategy_state is not None
        if not pair_window_is_open(pair, launched_at=self.strategy_state.launched_at, now=current_time):
            return self._result(state, events, "pair outside time window")
        if market is None or not market.ready:
            return self._result(state, events, "public market state is not ready")

        hooked, intents = step_pair(
            state,
            EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=current_time,
                symbol=self.symbol,
            ),
        )
        events.append(PairCycleEvent(pair.name, hooked, "head hooked"))
        if self.dry_run:
            self._save_pair_state(pair.name, hooked)
            return self._result(hooked, events, "dry run stopped before submission")

        commands = plan_runtime_commands(hooked, intents, symbol=cast(Symbol, self.symbol))
        if not commands:
            self._save_pair_state(pair.name, hooked)
            return self._result(hooked, events, "no command emitted")

        client_order_id = head_client_order_id(pair, at=current_time)
        submission_ack = await asyncio.to_thread(
            self._submit_head,
            pair,
            market,
            client_order_id,
        )
        submitted, _ = step_pair(
            hooked,
            EggMove(
                kind=EggMoveKind.HEAD_SUBMITTED,
                occurred_at=datetime.now(timezone.utc),
                symbol=self.symbol,
                order=head_order_dict(pair, client_order_id=client_order_id),
                reply=broker_reply_from_ack(submission_ack, client_order_id=client_order_id),
            ),
        )
        events.append(PairCycleEvent(pair.name, submitted, "head submitted"))
        self._save_pair_state(pair.name, submitted)
        return self._result(submitted, events, "head submitted; awaiting private confirmation")

    def _state_for_pair(self, pair: OrderPairSpec, now: datetime) -> PairCycleState:
        if self.strategy_state is None:
            self.strategy_state = StrategyState(launched_at=now, pairs={})
        existing = self.strategy_state.pairs.get(pair.name)
        if existing is None:
            initial = PairCycleState(pair=pair)
            self._save_pair_state(pair.name, initial)
            return initial
        if existing.pair != pair:
            migrated = replace(existing, pair=pair)
            self._save_pair_state(pair.name, migrated)
            return migrated
        return existing

    def _save_pair_state(self, pair_name: str, state: PairCycleState) -> None:
        assert self.strategy_state is not None
        self.strategy_state = replace(
            self.strategy_state,
            pairs={**self.strategy_state.pairs, pair_name: state},
        )

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


def step_pair(
    state: PairCycleState,
    move: EggMove,
) -> tuple[PairCycleState, tuple[PairIntent, ...]]:
    """Pure reducer for one pair transition.

    A reducer maps current immutable state plus one typed move to next immutable
    state and emitted command intents, with no side effects.
    """
    if state.head_state in {HeadState.FAILED, HeadState.CLOSED}:
        return state, ()

    if move.kind == EggMoveKind.HEAD_HOOKED:
        if state.head_state != HeadState.LATENT:
            return state, ()
        next_state = replace(state, head_state=HeadState.HOOKED)
        return next_state, (PairIntent(PairIntentKind.PLACE_HEAD),)

    if move.kind == EggMoveKind.HEAD_SUBMITTED:
        next_state = replace(
            state,
            head_state=HeadState.SUBMITTED,
            head_identity=head_identity_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.NOT_PLAYED_NOR_CANCELED:
        next_state = replace(
            state,
            head_state=HeadState.NEW,
            head_identity=head_identity_from_move(state, move),
            played_quantity=played_quantity_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.NOT_PLAYED_CANCELED:
        next_state = replace(
            state,
            head_state=HeadState.FAILED,
            head_identity=head_identity_from_move(state, move),
            tail_state=TailState.LATENT,
            tail_mode=None,
            played_quantity=played_quantity_from_move(state, move),
        )
        return next_state, ()

    if move.kind == EggMoveKind.PLAYED_NOT_CANCELED:
        played_quantity = played_quantity_from_move(state, move)
        if (
            state.head_state == HeadState.LIVING
            and state.played_quantity == played_quantity
            and state.tail_state in {TailState.SUBMITTED, TailState.LIVING}
        ):
            return state, ()
        next_state = replace(
            state,
            head_state=HeadState.LIVING,
            head_identity=head_identity_from_move(state, move),
            tail_state=TailState.LIVING,
            tail_mode=TailMode.FLAPPING,
            played_quantity=played_quantity,
        )
        return next_state, (_tail_intent_for_state(next_state),)

    if move.kind == EggMoveKind.PLAYED_AND_CANCELED:
        played_quantity = played_quantity_from_move(state, move)
        if (
            state.head_state == HeadState.CLOSED
            and state.played_quantity == played_quantity
            and state.tail_mode == TailMode.FLYING
        ):
            return state, ()
        next_state = replace(
            state,
            head_state=HeadState.CLOSED,
            head_identity=head_identity_from_move(state, move),
            tail_state=_next_closed_tail_state(state),
            tail_mode=TailMode.FLYING,
            played_quantity=played_quantity,
        )
        return next_state, (_tail_intent_for_state(next_state),)

    return state, ()


def _tail_intent_for_state(state: PairCycleState) -> PairIntent:
    if state.tail_identity is not None or state.tail_state in {
        TailState.SUBMITTED,
        TailState.LIVING,
    }:
        return PairIntent(PairIntentKind.AMEND_TAIL)
    return PairIntent(PairIntentKind.PLACE_TAIL)


def _next_closed_tail_state(state: PairCycleState) -> TailState:
    """Choisit l'etat du tail ferme selon son activation precedente."""
    if state.tail_state in {TailState.SUBMITTED, TailState.LIVING}:
        return TailState.LIVING
    return TailState.HOOKED


def resolve_quantity(pair: OrderPairSpec) -> float:
    quantity = pair.head_quantity
    if quantity is None or quantity <= 0:
        raise ValueError(f"Order pair '{pair.name}' needs a positive head quantity")
    return float(to_decimal(quantity))


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
        reply["cumQty"] = float(to_decimal(ack.executed_qty))  # type: ignore[typeddict-unknown-key]
    if ack.orig_qty is not None:
        reply["orderQty"] = float(to_decimal(ack.orig_qty))  # type: ignore[typeddict-unknown-key]
    return reply


def head_identity_from_move(
    state: PairCycleState,
    move: EggMove,
) -> OrderIdentity | None:
    reply = move.reply or {}
    order = move.order or {}
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


def played_quantity_from_move(state: PairCycleState, move: EggMove) -> Decimal | None:
    """Read played quantity from move payload and return unknown when missing."""
    reply = move.reply or {}
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = reply.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            parsed = to_decimal(value)
            return parsed if parsed >= Decimal("0") else Decimal("0")
    return None


def egg_move_from_confirmed_head(
    pair: OrderPairSpec,
    head: ConfirmedOrder,
    *,
    symbol: str,
) -> EggMove:
    return EggMove(
        kind=classify_confirmed_move(head),
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        reply={
            "orderID": head.identity.exchange_order_id or "",
            "clOrdID": head.identity.client_order_id or "",
            "ordStatus": head.state.value,
            "execType": head.reason.value,
            "cumQty": float(head.filled_quantity),
            "orderQty": float(head.total_quantity),
        },
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
