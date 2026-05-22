"""Async active strategy runtime over Orange, Chronos, Janus, and Ogun.

Purpose: own the active execution loop for dry-run, simulated execution, and
live/demo command submission through the typed bot stack.
Inputs: canonical strategy state, typed initial events, and an executor.
Outputs: final strategy state, emitted commands, and supervisor notices.
Side effects: async queue flow and optional command execution through an
executor boundary.
Important types: `Chronos`, `EggMove`, `RuntimeCommand`.
Role: interpreter shell.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count
from typing import Protocol, cast

from kolabi.bot.chronos import Chronos, ChronosNotice
from kolabi.bot.domain import EggMove, OrderPairSpec, PairCycleState, StrategySpec, StrategyState
from kolabi.bot.ids import head_client_order_id
from kolabi.bot.orange import (
    head_hooked_event,
    head_submitted_from_ack,
    simulated_private_fill_from_submission,
)
from kolabi.shared.core.bargain import Bargain
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    OrderDict,
    OrderRole,
    PlaceOrderCommandRequest,
    Price,
    RuntimeCommand,
    RuntimeCommandKind,
    Symbol,
    OrderQty,
    to_decimal,
)
from kolabi.runtime.kola.ogun_executor import execute_runtime_command


class CommandExecutor(Protocol):
    async def execute(self, command: RuntimeCommand) -> OrderAck: ...


@dataclass(frozen=True)
class StrategyRunResult:
    state: StrategyState
    commands: tuple[RuntimeCommand, ...]
    notices: tuple[ChronosNotice, ...]


class LegacyOgunExecutor:
    """Executor backed by Bargain plus legacy Ogun helper dispatch."""

    def __init__(self, bargain: Bargain, *, amend_absdelta: float = 1.0) -> None:
        self.bargain = bargain
        self.amend_absdelta = amend_absdelta

    async def execute(self, command: RuntimeCommand) -> OrderAck:
        reply = await asyncio.to_thread(
            execute_runtime_command,
            self.bargain,
            command,
            amend_absdelta=self.amend_absdelta,
        )
        return _ack_from_reply(reply, command)


class SimulatedExecutor:
    """Deterministic executor used by `--simulate`."""

    def __init__(self) -> None:
        self._ids = count(1)

    async def execute(self, command: RuntimeCommand) -> OrderAck:
        request = command.request
        qty: OrderQty | float | None = None
        price: Price | float | None = None
        side = None
        if isinstance(request, PlaceOrderCommandRequest):
            qty = _ack_quantity(request.orderQty)
            price = _ack_price(request.price if request.price is not None else request.stopPx)
            side = request.side
        return OrderAck(
            order_id=f"SIM-{next(self._ids)}",
            status="New",
            price=price,
            orig_qty=qty,
            executed_qty=0.0,
            side=side,
        )


class StrategyRuntime:
    """Foreground async runtime that drives the active strategy path."""

    def __init__(
        self,
        *,
        strategy: StrategySpec,
        symbol: str,
        executor: CommandExecutor | None = None,
        simulate: bool = False,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.executor = executor
        self.simulate = simulate
        self.state = StrategyState(
            launched_at=datetime.now(timezone.utc),
            pairs={pair.name: PairCycleState(pair=pair) for pair in strategy.pairs},
            strategy_id=strategy.name,
        )
        self.chronos = Chronos(state=self.state)

    async def plan(self) -> StrategyRunResult:
        initial_events = self._initial_events()
        commands = self.chronos.process_events(initial_events)
        self.state = self.chronos.state
        return StrategyRunResult(
            state=self.state,
            commands=commands,
            notices=tuple(self.chronos.notices),
        )

    async def run(self) -> StrategyRunResult:
        commands: list[RuntimeCommand] = []
        pending_events = list(self._initial_events())
        while pending_events:
            emitted = self.chronos.process_events(pending_events)
            self.state = self.chronos.state
            pending_events = []
            if not emitted:
                continue
            if self.executor is None:
                commands.extend(emitted)
                continue
            for command in emitted:
                prepared = self._prepare_command(command)
                ack = await self.executor.execute(prepared)
                commands.append(prepared)
                followups = self._followup_events(prepared, ack)
                pending_events.extend(followups)
        return StrategyRunResult(
            state=self.chronos.state,
            commands=tuple(commands),
            notices=tuple(self.chronos.notices),
        )

    def _initial_events(self) -> list[EggMove]:
        current_time = datetime.now(timezone.utc)
        return [
            head_hooked_event(
                pair_name=pair.name,
                symbol=self.symbol,
                occurred_at=current_time,
            )
            for pair in self.strategy.pairs
        ]

    def _prepare_command(self, command: RuntimeCommand) -> RuntimeCommand:
        if (
            command.role == OrderRole.HEAD
            and command.kind == RuntimeCommandKind.PLACE
            and isinstance(command.request, PlaceOrderCommandRequest)
            and command.request.clOrdID is None
        ):
            clordid = head_client_order_id(
                self.state.pairs[command.request.pair_name].pair,
                at=datetime.now(timezone.utc),
            )
            request = replace(command.request, clOrdID=clordid)
            order = dict(command.order or {})
            order["clOrdID"] = clordid
            typed_order = _typed_order(order)
            return replace(command, request=request, order=typed_order, legacy_order=typed_order)
        return command

    def _followup_events(self, command: RuntimeCommand, ack: OrderAck) -> tuple[EggMove, ...]:
        if command.role != OrderRole.HEAD or command.kind != RuntimeCommandKind.PLACE:
            return ()
        pair_name = command.pair_name or (command.request.pair_name if command.request is not None else None)
        if pair_name is None:
            return ()
        client_order_id = getattr(command.request, "clOrdID", None)
        submitted = head_submitted_from_ack(
            pair_name=pair_name,
            symbol=self.symbol,
            ack=ack,
            client_order_id=client_order_id,
            occurred_at=datetime.now(timezone.utc),
        )
        if not self.simulate:
            return (submitted,)
        played_quantity = _played_quantity_from_request(command.request)
        confirmed = simulated_private_fill_from_submission(
            submitted,
            played_quantity=played_quantity,
            closed=True,
        )
        return (submitted, confirmed)


def _ack_from_reply(reply: object, command: RuntimeCommand) -> OrderAck:
    if isinstance(reply, OrderAck):
        return reply
    if isinstance(reply, dict):
        return OrderAck(
            order_id=str(reply.get("orderID", "")),
            status=str(reply.get("ordStatus", "New")),
            price=reply.get("price") if isinstance(reply.get("price"), (int, float)) else None,
            orig_qty=reply.get("orderQty") if isinstance(reply.get("orderQty"), (int, float)) else None,
            executed_qty=reply.get("cumQty") if isinstance(reply.get("cumQty"), (int, float)) else None,
            side=str(reply.get("side")) if reply.get("side") is not None else None,
        )
    return OrderAck(
        order_id=f"ACK-{datetime.now(timezone.utc).timestamp()}",
        status="New",
        side=getattr(command.request, "side", None),
    )


def _played_quantity_from_request(request: object | None) -> float:
    if isinstance(request, PlaceOrderCommandRequest) and request.orderQty is not None:
        return float(request.orderQty)
    return 0.0


def _ack_price(value: Price | float | object | None) -> Price | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (Decimal, str)):
        return Price(to_decimal(value))
    raise TypeError(f"Unsupported ack price value type: {type(value)!r}")


def _ack_quantity(value: object | None) -> OrderQty | float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (Decimal, str)):
        return OrderQty(to_decimal(value))
    raise TypeError(f"Unsupported ack quantity value type: {type(value)!r}")


def _typed_order(order: dict[str, object]) -> OrderDict:
    return cast(OrderDict, order)
