"""Async persistent strategy runtime over Dragon, Chronos, Horus, and Ogun.

Purpose: own the active supervisor loop for simulated and live/demo execution
through the typed bot stack.
Inputs: canonical strategy state, real event sources, and an executor.
Outputs: final strategy state, emitted bot commands, and supervisor notices.
Side effects: async queue flow and optional command execution through an
executor boundary.
Important types: `Chronos`, `EggMove`, algebraic bot commands.
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
from kolabi.bot.domain import (
    EggMove,
    HeadState,
    OrderIdentity,
    PairCycleState,
    StrategySpec,
    StrategyState,
    TailState,
)
from kolabi.bot.ids import head_client_order_id, tail_client_order_id
from kolabi.bot.order_building import head_order_dict
from kolabi.bot.pricing import reference_price
from kolabi.bot.dragon import (
    MarketSnapshotFact,
    head_hooked_event,
    head_hooked_from_market_snapshot,
    head_move_from_private_fact,
    head_submitted_from_ack,
    market_tick_from_market_snapshot,
    private_order_fact_from_record,
    simulated_private_fill_from_submission,
    tail_submitted_from_ack,
)
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    DragonSong,
    OrderDict,
    OrderQty,
    PlaceOrderCommandRequest,
    PlaceHeadCommand,
    PlaceTailCommand,
    Price,
    PrivateOrderRecord,
    Symbol,
    to_decimal,
)


class CommandExecutor(Protocol):
    async def execute(self, command: DragonSong) -> OrderAck: ...


class RuntimeEventSource(Protocol):
    async def pump(self, runtime: "RuntimeQueueLike") -> None: ...


class RuntimeQueueLike(Protocol):
    symbol: str
    state: StrategyState
    running: bool

    @property
    def all_pairs_terminal(self) -> bool: ...

    async def enqueue(self, event: EggMove) -> None: ...

    def record_targets_head(self, record: object) -> bool: ...

    def head_pair_state_for_record(self, record: object) -> PairCycleState | None: ...


class PublicMarketStateReader(Protocol):
    """Adapter port for public market facts; the bot core does not know the DB."""

    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    recorded_at: str | None


class PublicRuntimeStateReader(Protocol):
    """Reads strategy-facing public market state from any backing store."""

    def fetch_market_state(
        self, symbol: str | None = None
    ) -> PublicMarketStateReader: ...


class PrivateOrderStateReader(Protocol):
    """Reads strategy-facing private order records from any backing store."""

    def fetch_private_orders_since(
        self,
        *,
        after_local_timestamp: datetime | None = None,
        after_local_id: int | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> tuple[PrivateOrderRecord, ...]: ...


class StrategyRuntimeLike(RuntimeQueueLike, Protocol):
    strategy: StrategySpec


@dataclass(frozen=True)
class StrategyRunResult:
    state: StrategyState
    commands: tuple[DragonSong, ...]
    notices: tuple[ChronosNotice, ...]


class SimulatedExecutor:
    """Deterministic executor used by `--simulate`."""

    def __init__(self) -> None:
        self._ids = count(1)

    async def execute(self, command: DragonSong) -> OrderAck:
        qty: OrderQty | float | None = None
        price: Price | float | None = None
        side = None
        if isinstance(command, (PlaceHeadCommand, PlaceTailCommand)):
            qty = _ack_quantity(command.request.orderQty)
            price = _ack_price(
                command.request.price
                if command.request.price is not None
                else command.request.stopPx
            )
            side = command.request.side
        return OrderAck(
            order_id=f"SIM-{next(self._ids)}",
            status="New",
            price=price,
            orig_qty=qty,
            executed_qty=0.0,
            side=side,
        )


class StaticHookSource:
    """One-shot source for planner/tests/simulation fallback."""

    async def pump(self, runtime: StrategyRuntimeLike) -> None:
        current_time = datetime.now(timezone.utc)
        for pair in runtime.strategy.pairs:
            await runtime.enqueue(
                head_hooked_event(
                    pair_name=pair.name,
                    symbol=runtime.symbol,
                    occurred_at=current_time,
                )
            )


class KrakenPublicTriggerSource:
    def __init__(
        self,
        client: PublicRuntimeStateReader,
        *,
        poll_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self._seen_event_ids: set[str] = set()

    async def pump(self, runtime: RuntimeQueueLike) -> None:
        while runtime.running:
            market = self.client.fetch_market_state(runtime.symbol)
            snapshot = MarketSnapshotFact(
                symbol=runtime.symbol,
                best_bid=market.best_bid,
                best_ask=market.best_ask,
                mid_price=market.mid_price,
                occurred_at=datetime.now(timezone.utc),
            )
            for pair_name, pair_state in runtime.state.pairs.items():
                if pair_state.head_state == HeadState.LATENT:
                    move = head_hooked_from_market_snapshot(
                        pair=pair_state.pair,
                        launched_at=runtime.state.launched_at,
                        snapshot=snapshot,
                    )
                    event_prefix = "public-hook"
                else:
                    if pair_state.tail_trail is None or pair_state.tail_state not in {
                        TailState.HOOKED,
                        TailState.SUBMITTED,
                        TailState.LIVING,
                    }:
                        continue
                    move = market_tick_from_market_snapshot(
                        pair=pair_state.pair,
                        snapshot=snapshot,
                    )
                    event_prefix = "public-market"
                if move is None:
                    continue
                event_id = f"{event_prefix}:{pair_name}:{market.recorded_at or snapshot.occurred_at.isoformat()}"
                if event_id in self._seen_event_ids:
                    continue
                self._seen_event_ids.add(event_id)
                await runtime.enqueue(replace(move, event_id=event_id))
            if runtime.all_pairs_terminal:
                return
            await asyncio.sleep(self.poll_seconds)


class KrakenPrivateOrderPollingSource:
    def __init__(
        self,
        client: PrivateOrderStateReader,
        *,
        public_client: PublicRuntimeStateReader | None = None,
        poll_seconds: float = 1.0,
    ) -> None:
        self.client = client
        self.public_client = public_client
        self.poll_seconds = poll_seconds
        self.after_local_timestamp: datetime | None = None
        self.after_local_id: int | None = None

    async def pump(self, runtime: RuntimeQueueLike) -> None:
        while runtime.running:
            records = self.client.fetch_private_orders_since(
                after_local_timestamp=self.after_local_timestamp,
                after_local_id=self.after_local_id,
                symbol=runtime.symbol,
            )
            for record in records:
                occurred_at = _record_timestamp(record)
                if occurred_at is not None:
                    self.after_local_timestamp = occurred_at
                self.after_local_id = record.local_id
                pair_state = runtime.head_pair_state_for_record(record)
                if pair_state is None:
                    continue
                fact = private_order_fact_from_record(record)
                move = head_move_from_private_fact(fact)
                move = self._with_reference_price(move, pair_state, runtime.symbol)
                event_id = (
                    f"private-order:{record.local_id}"
                    if record.local_id is not None
                    else None
                )
                await runtime.enqueue(replace(move, event_id=event_id))
            if runtime.all_pairs_terminal:
                return
            await asyncio.sleep(self.poll_seconds)

    def _with_reference_price(
        self,
        move: EggMove,
        pair_state: PairCycleState,
        symbol: str,
    ) -> EggMove:
        """Attach side-aware public reference price for relative tail initialisation."""
        public_client = self.public_client
        if public_client is None and hasattr(self.client, "fetch_market_state"):
            public_client = cast(PublicRuntimeStateReader, self.client)
        if public_client is None:
            return move
        market = public_client.fetch_market_state(symbol)
        reference = reference_price(pair_state.pair.head.side, market)
        if reference <= 0:
            return move
        reply = dict(move.reply or {})
        reply["reference_price"] = reference
        return replace(move, reply=reply)


class StrategyRuntime:
    """Persistent async supervisor that owns the active strategy path."""

    def __init__(
        self,
        *,
        strategy: StrategySpec,
        symbol: str,
        executor: CommandExecutor | None = None,
        public_source: RuntimeEventSource | None = None,
        private_source: RuntimeEventSource | None = None,
        simulate: bool = False,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.executor = executor
        self.public_source = public_source
        self.private_source = private_source
        self.simulate = simulate
        launched_at = datetime.now(timezone.utc)
        self.state = StrategyState(
            launched_at=launched_at,
            pairs={pair.name: self._pair_state(pair) for pair in strategy.pairs},
            strategy_id=strategy.name,
        )
        self.chronos = Chronos(state=self.state)
        self.event_queue: asyncio.Queue[EggMove] = asyncio.Queue()
        self.commands: list[DragonSong] = []
        self.running = False
        self._tasks: list[asyncio.Task[None]] = []

    def _pair_state(self, pair):
        from kolabi.bot.domain import PairCycleState

        return PairCycleState(pair=pair)

    @property
    def all_pairs_terminal(self) -> bool:
        return all(
            pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED}
            for pair_state in self.state.pairs.values()
        )

    async def enqueue(self, event: EggMove) -> None:
        await self.event_queue.put(event)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        sources = [
            self.public_source or (StaticHookSource() if self.simulate else None),
            None if self.simulate else self.private_source,
        ]
        self._tasks = [
            asyncio.create_task(source.pump(self))
            for source in sources
            if source is not None
        ]

    async def stop(self) -> None:
        self.running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def run(self) -> StrategyRunResult:
        await self.start()
        try:
            while self.running:
                if self.all_pairs_terminal and self.event_queue.empty():
                    break
                try:
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=0.2)
                except TimeoutError:
                    continue
                for command in self.chronos.process_event(event):
                    prepared = self._prepare_command(command)
                    self.commands.append(prepared)
                    if self.executor is None:
                        continue
                    ack = await self.executor.execute(prepared)
                    for followup in self._followup_events(prepared, ack):
                        await self.enqueue(followup)
                self.state = self.chronos.state
        finally:
            await self.stop()
        return StrategyRunResult(
            state=self.chronos.state,
            commands=tuple(self.commands),
            notices=tuple(self.chronos.notices),
        )

    def record_targets_head(self, record) -> bool:
        return self.head_pair_state_for_record(record) is not None

    def head_pair_state_for_record(self, record) -> PairCycleState | None:
        for pair_state in self.state.pairs.values():
            tail_identity = pair_state.tail_identity
            if tail_identity is not None and _record_matches_identity(
                record, tail_identity
            ):
                return None
            head_identity = pair_state.head_identity
            if head_identity is not None and _record_matches_identity(
                record, head_identity
            ):
                return pair_state
        return None

    def _prepare_command(self, command: DragonSong) -> DragonSong:
        if isinstance(command, PlaceHeadCommand) and command.request.clOrdID is None:
            pair = self.state.pairs[command.request.pair_name].pair
            clordid = head_client_order_id(
                pair,
                at=datetime.now(timezone.utc),
            )
            request = replace(command.request, clOrdID=clordid)
            return replace(
                command,
                request=request,
                legacy_order=head_order_dict(pair, client_order_id=clordid),
            )
        if isinstance(command, PlaceTailCommand) and command.request.clOrdID is None:
            pair_state = self.state.pairs[command.request.pair_name]
            clordid = tail_client_order_id(
                pair_state.pair,
                at=datetime.now(timezone.utc),
            )
            request = replace(command.request, clOrdID=clordid)
            legacy_order = dict(command.legacy_order or {})
            legacy_order["clOrdID"] = clordid
            return replace(
                command,
                request=request,
                legacy_order=cast(OrderDict, legacy_order),
            )
        return command

    def _followup_events(self, command: DragonSong, ack: OrderAck) -> tuple[EggMove, ...]:
        if isinstance(command, PlaceTailCommand):
            submitted_tail = tail_submitted_from_ack(
                pair_name=command.pair_name,
                symbol=self.symbol,
                ack=ack,
                client_order_id=command.request.clOrdID,
                occurred_at=datetime.now(timezone.utc),
            )
            return (submitted_tail,)
        if not isinstance(command, PlaceHeadCommand):
            return ()
        submitted = head_submitted_from_ack(
            pair_name=command.pair_name,
            symbol=self.symbol,
            ack=ack,
            client_order_id=command.request.clOrdID,
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


def plan_strategy_once(
    *,
    strategy: StrategySpec,
    symbol: str,
) -> StrategyRunResult:
    state = StrategyState(
        launched_at=datetime.now(timezone.utc),
        pairs={pair.name: _pair_state_from_spec(pair) for pair in strategy.pairs},
        strategy_id=strategy.name,
    )
    chronos = Chronos(state=state)
    commands = chronos.process_events(
        tuple(
            head_hooked_event(
                pair_name=pair.name,
                symbol=symbol,
                occurred_at=state.launched_at,
            )
            for pair in strategy.pairs
        )
    )
    return StrategyRunResult(
        state=chronos.state,
        commands=commands,
        notices=tuple(chronos.notices),
    )


def _pair_state_from_spec(pair):
    from kolabi.bot.domain import PairCycleState

    return PairCycleState(pair=pair)


def _record_matches_identity(record, identity: OrderIdentity) -> bool:
    if record.client_order_id and identity.client_order_id == record.client_order_id:
        return True
    if (
        record.exchange_order_id
        and identity.exchange_order_id == record.exchange_order_id
    ):
        return True
    return False


def _record_timestamp(record) -> datetime | None:
    if record.local_timestamp is not None:
        return datetime.fromisoformat(record.local_timestamp)
    if record.source_timestamp is not None:
        return datetime.fromisoformat(record.source_timestamp)
    return None


def _played_quantity_from_request(
    request: PlaceOrderCommandRequest | object | None,
) -> float:
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
