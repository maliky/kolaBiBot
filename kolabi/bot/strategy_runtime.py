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
import logging
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from typing import Mapping, Protocol, assert_never, cast

from kolabi.bot.chronos import (
    Chronos,
    ChronosNotice,
    pair_dependency_satisfied,
    resolve_pair_name,
)
from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadState,
    OrderIdentity,
    OrderRole,
    PairCycleState,
    StrategySpec,
    StrategyState,
    TailState,
)
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
from kolabi.bot.ids import head_client_order_id, tail_client_order_id
from kolabi.bot.pricing import pair_window_has_ended, tail_reference_price
from kolabi.bot.telemetry import TailTelemetryRow
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    DragonSong,
    OrderDict,
    OrderQty,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    Price,
    PrivateOrderRecord,
    RuntimeCommandKind,
    Symbol,
    to_decimal,
)

_LOGGER = logging.getLogger("kola")
_DEFAULT_HEAD_FILL_REFERENCE_GRACE_SECONDS = 20.0


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

    @property
    def should_keep_sources_alive(self) -> bool: ...

    async def enqueue(self, event: EggMove) -> None: ...

    def pair_state_for_record(self, record: object) -> tuple[PairCycleState, OrderRole] | None: ...


class PublicMarketStateReader(Protocol):
    """Adapter port for public market facts; the bot core does not know the DB."""

    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    last_price: float | None
    mark_price: float | None
    index_price: float | None
    tick_size: float | None
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

    def fetch_private_fills_since(
        self,
        *,
        after_local_timestamp: datetime | None = None,
        after_local_id: int | None = None,
        symbol: str | None = None,
        limit: int = 200,
    ) -> tuple[PrivateOrderRecord, ...]: ...


class StrategyRuntimeLike(RuntimeQueueLike, Protocol):
    strategy: StrategySpec


class TailTelemetryWriter(Protocol):
    def record_rows(self, rows: tuple[TailTelemetryRow, ...]) -> None: ...


@dataclass(frozen=True)
class _CommandSlot:
    pair_name: str
    attempt_index: int
    role: str


@dataclass(frozen=True)
class _PendingPrivateRecord:
    record: PrivateOrderRecord
    first_seen_at: datetime
    is_fill: bool = False


@dataclass
class _PrivateCursor:
    after_local_timestamp: datetime | None = None
    after_local_id: int | None = None
    after_fill_timestamp: datetime | None = None
    after_fill_id: int | None = None
    initialised: bool = False


@dataclass(frozen=True)
class _InFlightCommand:
    command: DragonSong
    task: asyncio.Task[None]


@dataclass(frozen=True)
class _OrderLifecycleSnapshot:
    side: str | None = None
    filled_qty: Decimal | None = None
    filled_price: Decimal | None = None
    filled_at: datetime | None = None


@dataclass(frozen=True)
class _TailVisibilityWindow:
    pair_name: str
    attempt_index: int
    client_order_id: str | None
    exchange_order_id: str | None
    started_at: datetime
    deadline_at: datetime
    last_warned_at: datetime | None = None


@dataclass(frozen=True)
class _TailAmendPending:
    pair_name: str
    attempt_index: int
    desired_stop_price: Decimal
    client_order_id: str | None
    exchange_order_id: str | None
    started_at: datetime
    deadline_at: datetime


@dataclass(frozen=True)
class _HeadFillDeadline:
    pair_name: str
    attempt_index: int
    client_order_id: str | None
    exchange_order_id: str | None
    started_at: datetime
    deadline_at: datetime
    cancel_dispatched_at: datetime | None = None


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
                    symbol=_pair_symbol_from_pair(pair, runtime.symbol),
                    occurred_at=current_time,
                )
            )


class KrakenPublicTriggerSource:
    def __init__(
        self,
        client: PublicRuntimeStateReader,
        *,
        poll_seconds: float = 0.25,
    ) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self._seen_event_ids: set[str] = set()

    async def pump(self, runtime: RuntimeQueueLike) -> None:
        while runtime.running:
            for symbol in _active_runtime_symbols(runtime):
                market = self.client.fetch_market_state(symbol)
                snapshot = MarketSnapshotFact(
                    symbol=symbol,
                    best_bid=market.best_bid,
                    best_ask=market.best_ask,
                    mid_price=market.mid_price,
                    occurred_at=datetime.now(timezone.utc),
                    last_price=getattr(market, "last_price", None),
                    mark_price=getattr(market, "mark_price", None),
                    index_price=getattr(market, "index_price", None),
                    tick_size=getattr(market, "tick_size", None),
                )
                for pair_name, pair_state in runtime.state.pairs.items():
                    if _pair_symbol(pair_state, runtime.symbol) != symbol:
                        continue
                    if pair_state.head_state == HeadState.LATENT:
                        if not pair_dependency_satisfied(runtime.state, pair_state):
                            continue
                        move = head_hooked_from_market_snapshot(
                            pair_state=pair_state,
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
                    reference_key = ""
                    if move.reply is not None:
                        reference_key = f":{move.reply.get('reference_source', '')}:{move.reply.get('reference_price', '')}"
                    event_id = (
                        f"{event_prefix}:{symbol}:{pair_name}:{pair_state.attempt_index}:"
                        f"{market.recorded_at or snapshot.occurred_at.isoformat()}{reference_key}"
                    )
                    if event_id in self._seen_event_ids:
                        continue
                    self._seen_event_ids.add(event_id)
                    await runtime.enqueue(replace(move, event_id=event_id))
            if _runtime_sources_should_stop(runtime):
                return
            await asyncio.sleep(self.poll_seconds)


class KrakenPrivateOrderPollingSource:
    def __init__(
        self,
        client: PrivateOrderStateReader,
        *,
        poll_seconds: float = 0.25,
        head_fill_reference_grace_seconds: float = _DEFAULT_HEAD_FILL_REFERENCE_GRACE_SECONDS,
    ) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self.head_fill_reference_grace_seconds = max(
            0.0, head_fill_reference_grace_seconds
        )
        self._cursors: dict[str, _PrivateCursor] = {}
        self._pending_records: list[_PendingPrivateRecord] = []

    async def pump(self, runtime: RuntimeQueueLike) -> None:
        while runtime.running:
            now = datetime.now(timezone.utc)
            candidates = tuple(self._pending_records)
            self._pending_records = []
            for symbol in _active_runtime_symbols(runtime):
                cursor = self._cursor_for(symbol, runtime.state.launched_at)
                records = self.client.fetch_private_orders_since(
                    after_local_timestamp=cursor.after_local_timestamp,
                    after_local_id=cursor.after_local_id,
                    symbol=symbol,
                )
                fill_records = self.client.fetch_private_fills_since(
                    after_local_timestamp=cursor.after_fill_timestamp,
                    after_local_id=cursor.after_fill_id,
                    symbol=symbol,
                )
                active_client_ids, active_exchange_ids = self._active_identity_sets(
                    runtime,
                    symbol,
                )
                if active_client_ids or active_exchange_ids:
                    fetch_orders_by_identity = getattr(
                        self.client, "fetch_private_orders_for_identities", None
                    )
                    if callable(fetch_orders_by_identity):
                        identity_orders = fetch_orders_by_identity(
                            client_order_ids=active_client_ids,
                            exchange_order_ids=active_exchange_ids,
                            symbol=symbol,
                        )
                        records = self._merge_unique_private_records(records, identity_orders)
                    fetch_fills_by_identity = getattr(
                        self.client, "fetch_private_fills_for_identities", None
                    )
                    if callable(fetch_fills_by_identity):
                        identity_fills = fetch_fills_by_identity(
                            client_order_ids=active_client_ids,
                            exchange_order_ids=active_exchange_ids,
                            symbol=symbol,
                        )
                        fill_records = self._merge_unique_private_records(
                            fill_records,
                            identity_fills,
                        )
                candidates = candidates + tuple(
                    _PendingPrivateRecord(record=record, first_seen_at=now)
                    for record in records
                ) + tuple(
                    _PendingPrivateRecord(
                        record=record,
                        first_seen_at=now,
                        is_fill=True,
                    )
                    for record in fill_records
                )
                self._advance_cursor(cursor, records, fill_records)
            for pending_record in candidates:
                record = pending_record.record
                resolved = runtime.pair_state_for_record(record)
                if resolved is None:
                    self._pending_records.append(pending_record)
                    continue
                pair_state, role = resolved
                fact = private_order_fact_from_record(
                    record,
                    pair_name=pair_state.pair.name,
                )
                move = head_move_from_private_fact(fact)
                move = replace(move, role=role)
                move = self._with_reference_price(
                    move,
                    pair_state,
                    _pair_symbol(pair_state, runtime.symbol),
                )
                if self._must_wait_for_private_fill_reference(move, pair_state, role):
                    if self._reference_price_from_move(move) is None:
                        elapsed_seconds = max(
                            0.0,
                            (now - pending_record.first_seen_at).total_seconds(),
                        )
                        if elapsed_seconds < self.head_fill_reference_grace_seconds:
                            self._pending_records.append(pending_record)
                            continue
                        order_id = record.exchange_order_id or "-"
                        client_id = record.client_order_id or "-"
                        raise RuntimeError(
                            "head fill reference price missing for relative tail after grace window: "
                            f"pair={pair_state.pair.name} clOrdID={client_id} "
                            f"orderID={order_id} grace_seconds={self.head_fill_reference_grace_seconds:g}"
                        )
                event_id = (
                    self._private_record_event_id(
                        record,
                        is_fill=pending_record.is_fill,
                    )
                )
                await runtime.enqueue(replace(move, event_id=event_id))
            if _runtime_sources_should_stop(runtime):
                return
            await asyncio.sleep(self.poll_seconds)

    def _cursor_for(self, symbol: str, launched_at: datetime) -> _PrivateCursor:
        cursor = self._cursors.setdefault(symbol, _PrivateCursor())
        if not cursor.initialised:
            cursor.after_local_timestamp = launched_at - timedelta(seconds=5)
            cursor.after_local_id = None
            cursor.after_fill_timestamp = launched_at - timedelta(seconds=5)
            cursor.after_fill_id = None
            cursor.initialised = True
        return cursor

    @staticmethod
    def _advance_cursor(
        cursor: _PrivateCursor,
        records: tuple[PrivateOrderRecord, ...],
        fill_records: tuple[PrivateOrderRecord, ...],
    ) -> None:
        for record in records:
            occurred_at = _record_timestamp(record)
            if occurred_at is not None:
                cursor.after_local_timestamp = occurred_at
            cursor.after_local_id = record.local_id
        for record in fill_records:
            occurred_at = _record_timestamp(record)
            if occurred_at is not None:
                cursor.after_fill_timestamp = occurred_at
            cursor.after_fill_id = record.local_id

    @staticmethod
    def _active_identity_sets(
        runtime: RuntimeQueueLike,
        symbol: str,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        client_ids: set[str] = set()
        exchange_ids: set[str] = set()
        for pair_state in runtime.state.pairs.values():
            if _pair_symbol(pair_state, runtime.symbol) != symbol:
                continue
            for identity in (pair_state.head_identity, pair_state.tail_identity):
                if identity is None:
                    continue
                if identity.client_order_id:
                    client_ids.add(identity.client_order_id)
                if identity.exchange_order_id:
                    exchange_ids.add(identity.exchange_order_id)
        if isinstance(runtime, StrategyRuntime):
            for identity in runtime._command_identities().values():
                if identity.symbol is not None and identity.symbol != symbol:
                    continue
                if identity.client_order_id:
                    client_ids.add(identity.client_order_id)
                if identity.exchange_order_id:
                    exchange_ids.add(identity.exchange_order_id)
        return tuple(sorted(client_ids)), tuple(sorted(exchange_ids))

    @staticmethod
    def _merge_unique_private_records(
        primary: tuple[PrivateOrderRecord, ...],
        extra: tuple[PrivateOrderRecord, ...],
    ) -> tuple[PrivateOrderRecord, ...]:
        merged: dict[tuple[object, ...], PrivateOrderRecord] = {}
        for record in primary + extra:
            key = (
                record.local_id,
                record.exchange_order_id,
                record.client_order_id,
                record.local_timestamp,
                record.status,
                record.order_type,
            )
            merged[key] = record
        return tuple(merged.values())

    @staticmethod
    def _private_record_event_id(
        record: PrivateOrderRecord,
        *,
        is_fill: bool,
    ) -> str | None:
        prefix = "private-fill" if is_fill else "private-order"
        identity = (
            str(record.local_id)
            if record.local_id is not None
            else (record.exchange_order_id or record.client_order_id)
        )
        if identity is None:
            return None
        # One DB row can represent multiple lifecycle snapshots over time.
        # Include state fingerprint so open -> amend -> close emits distinct events.
        fingerprint = ":".join(
            (
                _event_atom(record.local_timestamp),
                _event_atom(record.source_timestamp),
                _event_atom(record.status),
                _event_atom(record.reason),
                _event_atom(record.price),
                _event_atom(record.stop_price),
                _event_atom(record.filled_quantity),
                _event_atom(record.quantity),
            )
        )
        return f"{prefix}:{identity}:{fingerprint}"

    def _with_reference_price(
        self,
        move: EggMove,
        pair_state: PairCycleState,
        symbol: str,
    ) -> EggMove:
        """Attach role-sensitive execution/trigger reference from private payload."""
        del symbol
        reply = dict(move.reply or {})
        role = _role_from_move(move)
        fill_price = None
        if role == OrderRole.HEAD:
            for key in ("price", "fillPrice", "avgPx", "lastPx", "executed_price"):
                if key in reply and isinstance(reply[key], (int, float, Decimal, str)):
                    fill_price = reply[key]
                    break
        else:
            for key in ("stopPx", "stop_price", "stopPrice"):
                if key in reply and isinstance(reply[key], (int, float, Decimal, str)):
                    fill_price = reply[key]
                    break
        del pair_state
        if isinstance(fill_price, (int, float, Decimal, str)) and to_decimal(fill_price) > 0:
            reply["reference_price"] = fill_price
            return replace(move, reply=reply)
        return move

    def _must_wait_for_private_fill_reference(
        self,
        move: EggMove,
        pair_state: PairCycleState,
        role: OrderRole,
    ) -> bool:
        if role != OrderRole.HEAD:
            return False
        if move.kind not in {EggMoveKind.PLAYED_NOT_CANCELED, EggMoveKind.PLAYED_AND_CANCELED}:
            return False
        if not _pair_uses_relative_tail(pair_state):
            return False
        if pair_state.tail_trail is not None:
            return False
        played = _played_quantity_from_move(move)
        if played is None or played <= 0:
            return False
        return True

    def _reference_price_from_move(self, move: EggMove) -> Decimal | None:
        payload = move.reply or {}
        value = payload.get("reference_price")
        if isinstance(value, (int, float, Decimal, str)):
            parsed = to_decimal(value)
            if parsed > 0:
                return parsed
        return None


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
        public_state_reader: PublicRuntimeStateReader | None = None,
        tail_telemetry_writer: TailTelemetryWriter | None = None,
        tail_telemetry_interval_seconds: float = 30.0,
        exchange: str = "kraken",
        environment: str = "demo",
        market_type: str = "futures",
        account_scope: str = "default",
        tail_visibility_timeout_seconds: float = 20.0,
        max_active_pairs: int = 4,
        simulate: bool = False,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.executor = executor
        self.public_source = public_source
        self.private_source = private_source
        self.public_state_reader = public_state_reader
        self.tail_telemetry_writer = tail_telemetry_writer
        self.tail_telemetry_interval_seconds = tail_telemetry_interval_seconds
        self.exchange = exchange
        self.environment = environment
        self.market_type = market_type
        self.account_scope = account_scope
        self.tail_visibility_timeout_seconds = max(0.1, float(tail_visibility_timeout_seconds))
        self.max_active_pairs = max(0, int(max_active_pairs))
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
        self._inflight_commands: dict[_CommandSlot, _InFlightCommand] = {}
        self._pending_commands: dict[_CommandSlot, deque[DragonSong]] = {}
        self._pending_head_commands: deque[DragonSong] = deque()
        self._command_errors: list[BaseException] = []
        self._legend_logged = False
        self._last_pair_updates: dict[str, tuple[str, ...]] = {}
        self._last_tail_metrics: dict[str, tuple[str, ...]] = {}
        self._head_order_lifecycle: dict[str, _OrderLifecycleSnapshot] = {}
        self._tail_order_lifecycle: dict[str, _OrderLifecycleSnapshot] = {}
        self._live_command_identities: dict[str, OrderIdentity] = {}
        self._head_fill_deadlines: dict[_CommandSlot, _HeadFillDeadline] = {}
        self._pending_tail_visibility: dict[_CommandSlot, _TailVisibilityWindow] = {}
        self._pending_tail_amends: dict[_CommandSlot, _TailAmendPending] = {}

    def _pair_state(self, pair):
        from kolabi.bot.domain import PairCycleState

        return PairCycleState(pair=pair)

    @property
    def all_pairs_terminal(self) -> bool:
        now = datetime.now(timezone.utc)
        return all(
            _pair_runtime_complete(
                pair_state,
                launched_at=self.state.launched_at,
                now=now,
            )
            for pair_state in self.state.pairs.values()
        )

    @property
    def should_keep_sources_alive(self) -> bool:
        return bool(self.chronos.pending_repeats)

    async def enqueue(self, event: EggMove) -> None:
        await self.event_queue.put(event)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._log_runtime_legend_once()
        sources = [
            self.public_source or (StaticHookSource() if self.simulate else None),
            None if self.simulate else self.private_source,
        ]
        self._tasks = [
            asyncio.create_task(source.pump(self))
            for source in sources
            if source is not None
        ]
        if (
            not self.simulate
            and self.public_state_reader is not None
            and self.tail_telemetry_writer is not None
        ):
            self._tasks.append(asyncio.create_task(self._pump_tail_telemetry()))

    async def stop(self) -> None:
        self.running = False
        for task in self._tasks:
            task.cancel()
        for entry in self._inflight_commands.values():
            entry.task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._inflight_commands:
            await asyncio.gather(
                *(entry.task for entry in self._inflight_commands.values()),
                return_exceptions=True,
            )
        self._tasks.clear()
        self._inflight_commands.clear()
        self._pending_commands.clear()
        self._pending_head_commands.clear()
        self._head_fill_deadlines.clear()
        self._pending_tail_visibility.clear()
        self._pending_tail_amends.clear()

    async def run(self) -> StrategyRunResult:
        await self.start()
        try:
            while self.running:
                current_time = datetime.now(timezone.utc)
                self.chronos.expire_pending(now=current_time)
                self._reap_source_tasks()
                self._reap_command_tasks()
                if self._command_errors:
                    raise self._command_errors[0]
                self._prune_live_command_identities()
                self._check_head_fill_deadlines(current_time)
                self._check_tail_visibility_deadlines(current_time)
                self._check_tail_amend_deadlines(current_time)
                repeat_commands = self.chronos.activate_ready_repeats(
                    symbol=self.symbol,
                    now=current_time,
                )
                if repeat_commands or self.chronos.state is not self.state:
                    previous_pairs = dict(self.state.pairs)
                    previous_state = self.state
                    self.state = self.chronos.state
                    self._log_repeat_attempts(previous_state.pairs)
                if repeat_commands:
                    self._log_repeat_start(repeat_commands)
                    self._dispatch_commands(repeat_commands)
                    self._log_living_updates(previous_pairs)
                    self._drain_pending_head_commands()
                if (
                    self.all_pairs_terminal
                    and self.event_queue.empty()
                    and not self._inflight_commands
                    and not self._pending_commands
                    and not self._pending_head_commands
                    and not self.chronos.pending_repeats
                ):
                    break
                try:
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=0.2)
                except TimeoutError:
                    continue
                previous_pairs = dict(self.state.pairs)
                previous_repeats = dict(self.chronos.pending_repeats)
                self._record_head_lifecycle(event)
                commands = self.chronos.process_event(event)
                self.state = self.chronos.state
                self._sync_head_fill_deadline(event)
                self._log_new_pending_repeats(previous_repeats)
                self._log_chain_releases(previous_pairs)
                self._dispatch_commands(commands)
                self._log_living_updates(previous_pairs)
                self._drain_pending_head_commands()
        finally:
            await self.stop()
        return StrategyRunResult(
            state=self.chronos.state,
            commands=tuple(self.commands),
            notices=tuple(self.chronos.notices),
        )

    def _dispatch_commands(self, commands: tuple[DragonSong, ...]) -> None:
        for command in commands:
            prepared = self._prepare_command(command)
            self.commands.append(prepared)
            if self.executor is None:
                continue
            slot = self._command_slot(prepared)
            if isinstance(prepared, PlaceHeadCommand) and not self._head_capacity_available(prepared):
                self._pending_head_commands.append(prepared)
                _LOGGER.info(
                    "HEAD_WAIT (%s#%s): active_pairs=%s max_active_pairs=%s",
                    prepared.pair_name,
                    slot.attempt_index,
                    self._active_pair_count(),
                    self.max_active_pairs,
                )
                continue
            if slot not in self._inflight_commands:
                self._launch_command(slot, prepared)
                continue
            pending = self._pending_commands.setdefault(slot, deque())
            if isinstance(prepared, AmendTailCommand):
                pending = deque(
                    command for command in pending if not isinstance(command, AmendTailCommand)
                )
                pending.append(prepared)
                self._pending_commands[slot] = pending
                continue
            pending.append(prepared)
            self._pending_commands[slot] = pending

    def _launch_command(self, slot: _CommandSlot, prepared: DragonSong) -> None:
        identity = self._command_identity_from_command(prepared)
        if identity is not None:
            self._live_command_identities[_identity_key(identity)] = identity
        self._on_command_dispatched(slot, prepared, identity)
        self._inflight_commands[slot] = _InFlightCommand(
            command=prepared,
            task=asyncio.create_task(self._execute_and_enqueue(prepared)),
        )

    async def _execute_and_enqueue(self, prepared: DragonSong) -> None:
        if self.executor is None:
            return
        try:
            ack = await self.executor.execute(prepared)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(prepared, CancelCommand):
                _LOGGER.warning(
                    "COMMAND_FAILED (%s#%s %s): %s",
                    prepared.pair_name,
                    self._command_slot(prepared).attempt_index,
                    prepared.reason,
                    _compact_error(exc),
                )
                return
            failure = _command_failure_event(
                prepared,
                symbol=str(prepared.symbol),
                occurred_at=datetime.now(timezone.utc),
                error=exc,
            )
            if failure is None:
                raise
            _LOGGER.warning(
                "COMMAND_FAILED (%s#%s %s): %s",
                prepared.pair_name,
                self._command_slot(prepared).attempt_index,
                prepared.reason,
                _compact_error(exc),
            )
            await self.enqueue(failure)
            return
        self._record_live_ack(prepared, ack)
        for followup in self._followup_events(prepared, ack):
            await self.enqueue(followup)

    def _reap_command_tasks(self) -> None:
        done_slots: list[_CommandSlot] = []
        for slot, entry in tuple(self._inflight_commands.items()):
            if not entry.task.done():
                continue
            try:
                entry.task.result()
            except asyncio.CancelledError:
                pass
            except BaseException as exc:
                self._command_errors.append(exc)
            done_slots.append(slot)
        for slot in done_slots:
            self._inflight_commands.pop(slot, None)
            self._dispatch_next_pending(slot)

    def _reap_source_tasks(self) -> None:
        alive_tasks: list[asyncio.Task[None]] = []
        for task in self._tasks:
            if not task.done():
                alive_tasks.append(task)
                continue
            try:
                task.result()
            except asyncio.CancelledError:
                continue
            except BaseException as exc:
                self._command_errors.append(exc)
                continue
            if self.running and not self.all_pairs_terminal and not self.simulate:
                self._command_errors.append(
                    RuntimeError("runtime event source stopped before strategy completed")
                )
        self._tasks = alive_tasks

    def _dispatch_next_pending(self, slot: _CommandSlot) -> None:
        pending = self._pending_commands.get(slot)
        if pending is None:
            return
        while pending:
            next_command = pending.popleft()
            if not self._command_slot_still_live(slot):
                continue
            self._launch_command(slot, next_command)
            if pending:
                self._pending_commands[slot] = pending
            else:
                self._pending_commands.pop(slot, None)
            return
        self._pending_commands.pop(slot, None)

    def _drain_pending_head_commands(self) -> None:
        if not self._pending_head_commands:
            return
        deferred: deque[DragonSong] = deque()
        while self._pending_head_commands:
            command = self._pending_head_commands.popleft()
            if not isinstance(command, PlaceHeadCommand):
                deferred.append(command)
                continue
            slot = self._command_slot(command)
            if not self._command_slot_still_live(slot):
                continue
            if not self._head_capacity_available(command):
                deferred.append(command)
                continue
            self._launch_command(slot, command)
        self._pending_head_commands = deferred

    def _head_capacity_available(self, command: PlaceHeadCommand) -> bool:
        if self.max_active_pairs <= 0:
            return True
        active_pairs = self._active_pair_names()
        if command.pair_name in active_pairs:
            return True
        return len(active_pairs) < self.max_active_pairs

    def _active_pair_count(self) -> int:
        return len(self._active_pair_names())

    def _active_pair_names(self) -> set[str]:
        now = datetime.now(timezone.utc)
        active: set[str] = set()
        for pair_name, pair_state in self.state.pairs.items():
            if pair_state.head_state == HeadState.LATENT:
                continue
            if _pair_runtime_complete(
                pair_state,
                launched_at=self.state.launched_at,
                now=now,
            ):
                continue
            active.add(pair_name)
        for slot, entry in self._inflight_commands.items():
            if isinstance(entry.command, (PlaceHeadCommand, PlaceTailCommand, AmendTailCommand)):
                active.add(slot.pair_name)
        return active

    def _command_slot(self, command: DragonSong) -> _CommandSlot:
        pair_name = command.pair_name
        pair_state = self.state.pairs.get(pair_name)
        attempt_index = 1 if pair_state is None else pair_state.attempt_index
        role = "head"
        if isinstance(command, (PlaceTailCommand, AmendTailCommand)):
            role = "tail"
        elif isinstance(command, CancelCommand):
            role = "cancel"
        return _CommandSlot(
            pair_name=pair_name,
            attempt_index=attempt_index,
            role=role,
        )

    def _command_slot_still_live(self, slot: _CommandSlot) -> bool:
        pair_state = self.state.pairs.get(slot.pair_name)
        if pair_state is None:
            return False
        if pair_state.attempt_index != slot.attempt_index:
            return False
        if slot.role == "tail":
            return pair_state.tail_state not in {None, TailState.CLOSED, TailState.FAILED}
        if slot.role == "head":
            return pair_state.head_state not in {HeadState.CLOSED, HeadState.FAILED}
        return True

    async def _pump_tail_telemetry(self) -> None:
        interval = max(self.tail_telemetry_interval_seconds, 1.0)
        while self.running:
            now = datetime.now(timezone.utc)
            rows = self._collect_tail_telemetry_rows(now)
            if rows and self.tail_telemetry_writer is not None:
                try:
                    self.tail_telemetry_writer.record_rows(rows)
                except Exception as exc:
                    _LOGGER.warning(
                        "tail telemetry persistence failed rows=%s error=%s",
                        len(rows),
                        _compact_error(exc),
                    )
            for row in rows:
                source = "unknown"
                market = (
                    None
                    if self.public_state_reader is None
                    else self.public_state_reader.fetch_market_state(row.symbol)
                )
                if market is not None:
                    source, _ = tail_reference_price(self.state.pairs[row.pair_name].pair, market)
                signature = (
                    str(self.state.pairs[row.pair_name].attempt_index),
                    row.head_state,
                    row.tail_state,
                    _fmt_compact_price(row.reference_price),
                    _fmt_compact_price(row.stop_price),
                    _fmt_compact_price(row.initial_distance),
                    _fmt_compact_price(row.current_distance),
                    row.last_tail_update_at.isoformat() if row.last_tail_update_at is not None else "-",
                    source,
                    _fmt_compact_price(None if market is None else getattr(market, "best_bid", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "best_ask", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "mid_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "last_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "mark_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "index_price", None)),
                )
                if self._last_tail_metrics.get(row.pair_name) == signature:
                    continue
                self._last_tail_metrics[row.pair_name] = signature
                _LOGGER.info(
                    "METRICS (%s#%s): (%s--%s) ref=%s stop=%s ID=%s CD=%s LU=%s src=%s px=B:%s A:%s MID:%s L:%s MK:%s I:%s",
                    row.pair_name,
                    signature[0],
                    row.head_state,
                    row.tail_state,
                    signature[3],
                    signature[4],
                    signature[5],
                    signature[6],
                    signature[7],
                    signature[8],
                    signature[9],
                    signature[10],
                    signature[11],
                    signature[12],
                    signature[13],
                    signature[14],
                )
            await asyncio.sleep(interval)

    def _collect_tail_telemetry_rows(self, now: datetime) -> tuple[TailTelemetryRow, ...]:
        reader = self.public_state_reader
        if reader is None:
            return ()
        rows: list[TailTelemetryRow] = []
        for pair_name, pair_state in self.state.pairs.items():
            if (
                pair_state.tail_trail is None
                or pair_state.tail_state not in {TailState.HOOKED, TailState.SUBMITTED, TailState.LIVING}
            ):
                continue
            symbol = _pair_symbol(pair_state, self.symbol)
            market = reader.fetch_market_state(symbol)
            _, ref = tail_reference_price(pair_state.pair, market)
            if ref <= 0:
                continue
            stop = pair_state.tail_trail.confirmed_stop_price
            if stop is None:
                continue
            current_distance = _tail_signed_distance(pair_state, to_decimal(ref), stop)
            rows.append(
                TailTelemetryRow(
                    exchange=self.exchange,
                    environment=self.environment,
                    market_type=self.market_type,
                    account_scope=self.account_scope,
                    strategy_id=self.state.strategy_id,
                    pair_name=pair_name,
                    symbol=symbol,
                    head_state=pair_state.head_state.value,
                    tail_state=pair_state.tail_state.value,
                    tail_mode=None if pair_state.tail_mode is None else pair_state.tail_mode.value,
                    reference_price=float(ref),
                    stop_price=float(stop),
                    initial_distance=float(pair_state.tail_trail.baseline_width),
                    current_distance=float(current_distance),
                    last_tail_update_at=pair_state.tail_trail.last_confirmed_at,
                    recorded_at=now,
                )
            )
        return tuple(rows)

    def _log_living_updates(self, previous_pairs: dict[str, PairCycleState]) -> None:
        for pair_name, current in self.state.pairs.items():
            previous = previous_pairs.get(pair_name)
            if previous is None:
                continue
            if (
                current.head_state == HeadState.FAILED
                and previous.head_state != HeadState.FAILED
            ):
                identity = current.head_identity
                played_qty = (
                    str(current.played_quantity)
                    if current.played_quantity is not None
                    else "-"
                )
                head_cancel_signature = (
                    str(current.attempt_index),
                    current.head_state.value,
                    played_qty,
                    "-"
                    if identity is None or identity.client_order_id is None
                    else identity.client_order_id,
                    "-"
                    if identity is None or identity.exchange_order_id is None
                    else identity.exchange_order_id,
                )
                if self._last_pair_updates.get(pair_name) != head_cancel_signature:
                    self._last_pair_updates[pair_name] = head_cancel_signature
                    _LOGGER.info(
                        "HEAD_CANCELLED (%s#%s): PQ=%s HCID=%s HOID=%s",
                        pair_name,
                        head_cancel_signature[0],
                        head_cancel_signature[2],
                        head_cancel_signature[3],
                        head_cancel_signature[4],
                    )
                continue
            if current.head_state not in {HeadState.LIVING, HeadState.CLOSED} and current.tail_state not in {
                TailState.LIVING,
                TailState.SUBMITTED,
            }:
                continue
            quantity_changed = current.played_quantity != previous.played_quantity
            stop_previous = _confirmed_tail_stop(previous)
            stop_current = _confirmed_tail_stop(current)
            desired_stop_previous = (
                None if previous.tail_trail is None else previous.tail_trail.current_stop_price
            )
            desired_stop = (
                None if current.tail_trail is None else current.tail_trail.current_stop_price
            )
            stop_changed = stop_current != stop_previous
            desired_stop_changed = desired_stop != desired_stop_previous
            state_changed = (
                current.head_state != previous.head_state
                or current.tail_state != previous.tail_state
            )
            if not (quantity_changed or stop_changed or desired_stop_changed or state_changed):
                continue
            head_lifecycle = self._head_order_lifecycle.get(pair_name, _OrderLifecycleSnapshot())
            update_signature: tuple[str, ...]
            message: str
            message_args: tuple[object, ...]
            transition = (
                current.head_state.value,
                current.tail_state.value if current.tail_state is not None else "-",
            )
            played_qty = str(current.played_quantity) if current.played_quantity is not None else "-"
            if transition == ("closed", "hooked"):
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(desired_stop),
                    head_lifecycle.side or "-",
                    _fmt_compact_price(head_lifecycle.filled_qty),
                    _fmt_compact_price(head_lifecycle.filled_price),
                    head_lifecycle.filled_at.isoformat()
                    if head_lifecycle.filled_at is not None
                    else "-",
                )
                message = (
                    "UPDATE (%s#%s): (%s--%s) PQ=%s DS=%s HFS=%s HFQ=%s HFP=%s HFT=%s"
                )
                message_args = (
                    pair_name,
                    update_signature[0],
                    update_signature[1],
                    update_signature[2],
                    update_signature[3],
                    update_signature[4],
                    update_signature[5],
                    update_signature[6],
                    update_signature[7],
                    update_signature[8],
                )
            elif transition[0] == "closed" and transition[1] in {"closed", "failed"}:
                tail_lifecycle = self._tail_order_lifecycle.get(
                    pair_name,
                    _OrderLifecycleSnapshot(),
                )
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(stop_current),
                    _fmt_compact_price(desired_stop),
                    tail_lifecycle.side or "-",
                    _fmt_compact_price(tail_lifecycle.filled_qty),
                    _fmt_compact_price(tail_lifecycle.filled_price),
                    tail_lifecycle.filled_at.isoformat()
                    if tail_lifecycle.filled_at is not None
                    else "-",
                )
                message = (
                    "UPDATE (%s#%s): (%s--%s) PQ=%s CS=%s DS=%s TFS=%s TFQ=%s TFP=%s TFT=%s"
                )
                message_args = (
                    pair_name,
                    update_signature[0],
                    update_signature[1],
                    update_signature[2],
                    update_signature[3],
                    update_signature[4],
                    update_signature[5],
                    update_signature[6],
                    update_signature[7],
                    update_signature[8],
                    update_signature[9],
                )
            elif transition in {("closed", "submitted"), ("closed", "living")}:
                tail_identity = current.tail_identity
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(stop_current),
                    _fmt_compact_price(desired_stop),
                    "-"
                    if tail_identity is None or tail_identity.client_order_id is None
                    else tail_identity.client_order_id,
                    "-"
                    if tail_identity is None or tail_identity.exchange_order_id is None
                    else tail_identity.exchange_order_id,
                    "-"
                    if current.tail_trail is None or current.tail_trail.last_confirmed_at is None
                    else current.tail_trail.last_confirmed_at.isoformat(),
                )
                message = (
                    "UPDATE (%s#%s): (%s--%s) PQ=%s CS=%s DS=%s TCID=%s TOID=%s TLU=%s"
                )
                message_args = (
                    pair_name,
                    update_signature[0],
                    update_signature[1],
                    update_signature[2],
                    update_signature[3],
                    update_signature[4],
                    update_signature[5],
                    update_signature[6],
                    update_signature[7],
                    update_signature[8],
                )
            else:
                update_signature = (
                    str(current.attempt_index),
                    transition[0],
                    transition[1],
                    played_qty,
                    _fmt_compact_price(stop_current),
                    _fmt_compact_price(desired_stop),
                )
                message = "UPDATE (%s#%s): (%s--%s) PQ=%s CS=%s DS=%s"
                message_args = (
                    pair_name,
                    update_signature[0],
                    update_signature[1],
                    update_signature[2],
                    update_signature[3],
                    update_signature[4],
                    update_signature[5],
                )
            if self._last_pair_updates.get(pair_name) == update_signature:
                continue
            self._last_pair_updates[pair_name] = update_signature
            _LOGGER.info(message, *message_args)

    def _log_runtime_legend_once(self) -> None:
        if self._legend_logged:
            return
        self._legend_logged = True
        _LOGGER.info(
            "RAPPEL: AI=attempt_index PU=pair_update HS=head_state TS=tail_state PQ=played_qty CS=confirmed_stop DS=desired_stop ID=initial_dist CD=current_dist LU=last_update TCID=tail_client_id TOID=tail_order_id TLU=tail_last_update HFS/HFQ/HFP/HFT=head_fill_fields(closed--hooked) TFS/TFQ/TFP/TFT=tail_fill_fields(closed--closed)"
        )

    def _record_head_lifecycle(self, move: EggMove) -> None:
        if move.role not in {OrderRole.HEAD, OrderRole.TAIL} or move.reply is None:
            return
        pair_name = move.pair_name or resolve_pair_name(self.state, move)
        if pair_name is None:
            return
        reply = move.reply
        side = reply.get("side")
        side_value = side.lower() if isinstance(side, str) and side else None
        filled_qty = _filled_quantity_from_move_payload(reply)
        filled_price = _head_fill_price_from_move_payload(reply)
        if side_value is None:
            pair_state = self.state.pairs.get(pair_name)
            if pair_state is not None:
                side = (
                    pair_state.pair.head.side
                    if move.role == OrderRole.HEAD
                    else pair_state.pair.tail.side
                )
                side_value = side.value
        snapshots = (
            self._head_order_lifecycle
            if move.role == OrderRole.HEAD
            else self._tail_order_lifecycle
        )
        previous = snapshots.get(pair_name, _OrderLifecycleSnapshot())
        snapshots[pair_name] = _OrderLifecycleSnapshot(
            side=side_value,
            filled_qty=filled_qty if filled_qty is not None else previous.filled_qty,
            filled_price=filled_price if filled_price is not None else previous.filled_price,
            filled_at=(
                move.occurred_at
                if (filled_qty is not None or filled_price is not None)
                else previous.filled_at
            ),
        )

    def _prune_live_command_identities(self) -> None:
        stale: list[str] = []
        for key, identity in self._live_command_identities.items():
            pair_state = self.state.pairs.get(identity.pair_name)
            if pair_state is None:
                stale.append(key)
                continue
            if identity.role == "head":
                if _identity_confirmed(identity, pair_state.head_identity):
                    stale.append(key)
                    continue
                if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED}:
                    stale.append(key)
                    continue
            elif identity.role == "tail":
                if _identity_confirmed(identity, pair_state.tail_identity):
                    stale.append(key)
                    continue
                if pair_state.tail_state in {None, TailState.CLOSED, TailState.FAILED}:
                    stale.append(key)
                    continue
            else:
                # Cancel correlation can be dropped once it cannot affect live exposure.
                if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED} and pair_state.tail_state in {
                    None,
                    TailState.LATENT,
                    TailState.CLOSED,
                    TailState.FAILED,
                }:
                    stale.append(key)
        for key in stale:
            self._live_command_identities.pop(key, None)

    def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
        record_symbol = _record_symbol(record)
        for identity in self._identity_map_from_pair_and_commands().values():
            if identity.role not in {"head", "tail"}:
                continue
            pair_state = self.state.pairs.get(identity.pair_name)
            if pair_state is None:
                continue
            if record_symbol is not None and _pair_symbol(pair_state, self.symbol) != record_symbol:
                continue
            if _record_matches_identity(record, identity):
                role = OrderRole.HEAD if identity.role == "head" else OrderRole.TAIL
                return pair_state, role
        for pair_state in self.state.pairs.values():
            if record_symbol is not None and _pair_symbol(pair_state, self.symbol) != record_symbol:
                continue
            tail_identity = pair_state.tail_identity
            if tail_identity is not None and _record_matches_identity(
                record, tail_identity
            ):
                return pair_state, OrderRole.TAIL
            head_identity = pair_state.head_identity
            if head_identity is not None and _record_matches_identity(
                record, head_identity
            ):
                return pair_state, OrderRole.HEAD
        return None

    def _command_identities(self) -> dict[str, OrderIdentity]:
        return self._identity_map_from_pair_and_commands()

    def _identity_map_from_pair_and_commands(self) -> dict[str, OrderIdentity]:
        identities: dict[str, OrderIdentity] = {}
        for pair_state in self.state.pairs.values():
            if pair_state.head_identity is not None:
                identities[_identity_key(pair_state.head_identity)] = pair_state.head_identity
            if pair_state.tail_identity is not None:
                identities[_identity_key(pair_state.tail_identity)] = pair_state.tail_identity
        identities.update(self._live_command_identities)
        for entry in self._inflight_commands.values():
            identity = self._command_identity_from_command(entry.command)
            if identity is not None:
                identities[_identity_key(identity)] = identity
        for queue in self._pending_commands.values():
            for command in queue:
                identity = self._command_identity_from_command(command)
                if identity is not None:
                    identities[_identity_key(identity)] = identity
        return identities

    @staticmethod
    def _command_identity_from_command(command: DragonSong) -> OrderIdentity | None:
        pair_name = command.pair_name
        if isinstance(command, CancelCommand):
            cancel_id = getattr(command.request, "clOrdID", None)
            if not cancel_id:
                return None
            return OrderIdentity(
                pair_name=pair_name,
                role="cancel",
                client_order_id=cancel_id,
                symbol=str(command.symbol),
            )
        client_order_id = getattr(command.request, "clOrdID", None)
        if not client_order_id:
            return None
        if isinstance(command, (PlaceHeadCommand, AmendHeadCommand)):
            role = "head"
        elif isinstance(command, (PlaceTailCommand, AmendTailCommand)):
            role = "tail"
        else:
            assert_never(command)
        exchange_order_id = getattr(command.request, "orderID", None)
        return OrderIdentity(
            pair_name=pair_name,
            role=role,
            client_order_id=client_order_id,
            exchange_order_id=exchange_order_id,
            symbol=str(command.symbol),
        )

    def _prepare_command(self, command: DragonSong) -> DragonSong:
        if isinstance(command, PlaceHeadCommand) and command.request.clOrdID is None:
            pair_state = self.state.pairs[command.request.pair_name]
            pair = pair_state.pair
            clordid = head_client_order_id(
                pair,
                attempt_index=pair_state.attempt_index,
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
        if isinstance(command, PlaceTailCommand) and command.request.clOrdID is None:
            pair_state = self.state.pairs[command.request.pair_name]
            clordid = tail_client_order_id(
                pair_state.pair,
                attempt_index=pair_state.attempt_index,
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

    def _record_live_ack(self, command: DragonSong, ack: OrderAck) -> None:
        if self.simulate:
            return
        identity = self._command_identity_from_command(command)
        if identity is None:
            return
        merged = OrderIdentity(
            pair_name=identity.pair_name,
            role=identity.role,
            client_order_id=identity.client_order_id,
            exchange_order_id=str(ack.order_id) if ack.order_id else identity.exchange_order_id,
            symbol=identity.symbol,
        )
        self._live_command_identities[_identity_key(merged)] = merged
        if isinstance(command, AmendTailCommand) and _ack_is_rejected(ack):
            pair_state = self.state.pairs.get(command.pair_name)
            attempt_index = 1 if pair_state is None else pair_state.attempt_index
            slot = _CommandSlot(
                pair_name=command.pair_name,
                attempt_index=attempt_index,
                role="tail",
            )
            self._pending_tail_amends.pop(slot, None)
            _LOGGER.warning(
                "AMEND_REJECTED (%s#%s): status=%s TCID=%s TOID=%s",
                command.pair_name,
                attempt_index,
                ack.status,
                merged.client_order_id or "-",
                merged.exchange_order_id or "-",
            )
        if not isinstance(command, PlaceTailCommand):
            return
        pair_state = self.state.pairs.get(command.pair_name)
        attempt_index = 1 if pair_state is None else pair_state.attempt_index
        slot = _CommandSlot(
            pair_name=command.pair_name,
            attempt_index=attempt_index,
            role="tail",
        )
        now = datetime.now(timezone.utc)
        self._pending_tail_visibility[slot] = _TailVisibilityWindow(
            pair_name=command.pair_name,
            attempt_index=attempt_index,
            client_order_id=merged.client_order_id,
            exchange_order_id=merged.exchange_order_id,
            started_at=now,
            deadline_at=now + timedelta(seconds=self.tail_visibility_timeout_seconds),
        )

    def _sync_head_fill_deadline(self, move: EggMove) -> None:
        if move.role != OrderRole.HEAD:
            return
        pair_name = move.pair_name or resolve_pair_name(self.state, move)
        if pair_name is None:
            return
        pair_state = self.state.pairs.get(pair_name)
        if pair_state is None:
            return
        slot = _CommandSlot(
            pair_name=pair_name,
            attempt_index=pair_state.attempt_index,
            role="head",
        )
        if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED}:
            self._head_fill_deadlines.pop(slot, None)
            return
        if pair_state.head_state not in {HeadState.NEW, HeadState.LIVING}:
            return
        timeout_minutes = pair_state.pair.timeout_minutes
        if timeout_minutes is None or timeout_minutes <= 0:
            return
        if slot in self._head_fill_deadlines:
            return
        identity = pair_state.head_identity
        if identity is None:
            return
        started_at = _as_utc_aware(move.occurred_at)
        deadline = _HeadFillDeadline(
            pair_name=pair_name,
            attempt_index=pair_state.attempt_index,
            client_order_id=identity.client_order_id,
            exchange_order_id=identity.exchange_order_id,
            started_at=started_at,
            deadline_at=started_at + timedelta(minutes=float(timeout_minutes)),
        )
        self._head_fill_deadlines[slot] = deadline
        _LOGGER.info(
            "HEAD_ACK (%s#%s): HCID=%s HOID=%s tOut_deadline=%s",
            pair_name,
            pair_state.attempt_index,
            identity.client_order_id or "-",
            identity.exchange_order_id or "-",
            deadline.deadline_at.isoformat(),
        )

    def _check_head_fill_deadlines(self, now: datetime) -> None:
        stale_slots: list[_CommandSlot] = []
        updated: dict[_CommandSlot, _HeadFillDeadline] = {}
        for slot, deadline in tuple(self._head_fill_deadlines.items()):
            pair_state = self.state.pairs.get(deadline.pair_name)
            if pair_state is None or pair_state.attempt_index != deadline.attempt_index:
                stale_slots.append(slot)
                continue
            if pair_state.head_state in {HeadState.CLOSED, HeadState.FAILED}:
                stale_slots.append(slot)
                continue
            if pair_state.head_state not in {HeadState.NEW, HeadState.LIVING}:
                continue
            if _as_utc_aware(now) < deadline.deadline_at:
                continue
            cancel_id = deadline.exchange_order_id or deadline.client_order_id
            if not cancel_id:
                _LOGGER.warning(
                    "HEAD_TIMEOUT (%s#%s): cannot_cancel missing_identity waited=%ss",
                    deadline.pair_name,
                    deadline.attempt_index,
                    f"{(_as_utc_aware(now) - deadline.started_at).total_seconds():.1f}",
                )
                continue
            cancel_slot = _CommandSlot(
                pair_name=deadline.pair_name,
                attempt_index=deadline.attempt_index,
                role="cancel",
            )
            if cancel_slot in self._inflight_commands:
                continue
            if (
                deadline.cancel_dispatched_at is not None
                and (_as_utc_aware(now) - deadline.cancel_dispatched_at).total_seconds()
                < self._head_cancel_retry_seconds()
            ):
                continue
            if deadline.cancel_dispatched_at is None:
                _LOGGER.warning(
                    "HEAD_TIMEOUT (%s#%s): waited=%ss HCID=%s HOID=%s",
                    deadline.pair_name,
                    deadline.attempt_index,
                    f"{(_as_utc_aware(now) - deadline.started_at).total_seconds():.1f}",
                    deadline.client_order_id or "-",
                    deadline.exchange_order_id or "-",
                )
            command = _head_timeout_cancel_command(
                pair_state,
                _pair_symbol(pair_state, self.symbol),
                cancel_id,
            )
            self._dispatch_commands((command,))
            updated[slot] = replace(deadline, cancel_dispatched_at=_as_utc_aware(now))
        for slot in stale_slots:
            self._head_fill_deadlines.pop(slot, None)
        self._head_fill_deadlines.update(updated)

    def _head_cancel_retry_seconds(self) -> float:
        return max(5.0, self.tail_visibility_timeout_seconds)

    def _check_tail_visibility_deadlines(self, now: datetime) -> None:
        stale_slots: list[_CommandSlot] = []
        delayed_slots: dict[_CommandSlot, _TailVisibilityWindow] = {}
        for slot, window in self._pending_tail_visibility.items():
            pair_state = self.state.pairs.get(window.pair_name)
            if pair_state is None or pair_state.attempt_index != window.attempt_index:
                stale_slots.append(slot)
                continue
            tail_identity = pair_state.tail_identity
            if tail_identity is not None and _identities_overlap(
                tail_identity,
                window.client_order_id,
                window.exchange_order_id,
            ):
                if window.last_warned_at is not None:
                    _LOGGER.info(
                        "TAIL_VISIBLE (%s#%s): TCID=%s TOID=%s waited=%ss",
                        window.pair_name,
                        window.attempt_index,
                        window.client_order_id or "-",
                        window.exchange_order_id or "-",
                        f"{(now - window.started_at).total_seconds():.1f}",
                    )
                stale_slots.append(slot)
                continue
            if pair_state.tail_state in {TailState.CLOSED, TailState.FAILED}:
                stale_slots.append(slot)
                continue
            if now < window.deadline_at:
                continue
            warn_after = max(5.0, self.tail_visibility_timeout_seconds)
            if (
                window.last_warned_at is not None
                and (now - window.last_warned_at).total_seconds() < warn_after
            ):
                continue
            _LOGGER.warning(
                "TAIL_PENDING (%s#%s): TCID=%s TOID=%s waited=%ss timeout=%ss",
                window.pair_name,
                window.attempt_index,
                window.client_order_id or "-",
                window.exchange_order_id or "-",
                f"{(now - window.started_at).total_seconds():.1f}",
                f"{self.tail_visibility_timeout_seconds:.1f}",
            )
            delayed_slots[slot] = replace(window, last_warned_at=now)
        for slot in stale_slots:
            self._pending_tail_visibility.pop(slot, None)
        self._pending_tail_visibility.update(delayed_slots)

    def _check_tail_amend_deadlines(self, now: datetime) -> None:
        stale_slots: list[_CommandSlot] = []
        for slot, pending in self._pending_tail_amends.items():
            pair_state = self.state.pairs.get(pending.pair_name)
            if pair_state is None or pair_state.attempt_index != pending.attempt_index:
                stale_slots.append(slot)
                continue
            if pair_state.tail_state in {None, TailState.CLOSED, TailState.FAILED}:
                stale_slots.append(slot)
                continue
            confirmed_stop = _confirmed_tail_stop(pair_state)
            if _prices_close(confirmed_stop, pending.desired_stop_price):
                stale_slots.append(slot)
                continue
            if now < pending.deadline_at:
                continue
            _LOGGER.warning(
                "AMEND_PENDING (%s#%s): CS=%s DS=%s TCID=%s TOID=%s waited=%ss",
                pending.pair_name,
                pending.attempt_index,
                _fmt_compact_price(confirmed_stop),
                _fmt_compact_price(pending.desired_stop_price),
                pending.client_order_id or "-",
                pending.exchange_order_id or "-",
                f"{(now - pending.started_at).total_seconds():.1f}",
            )
            stale_slots.append(slot)
        for slot in stale_slots:
            self._pending_tail_amends.pop(slot, None)

    def _on_command_dispatched(
        self,
        slot: _CommandSlot,
        command: DragonSong,
        identity: OrderIdentity | None,
    ) -> None:
        if isinstance(command, PlaceHeadCommand):
            pair_state = self.state.pairs.get(command.pair_name)
            attempt_index = slot.attempt_index if pair_state is None else pair_state.attempt_index
            _LOGGER.info(
                "HEAD_SENT (%s#%s): HCID=%s side=%s type=%s qty=%s price=%s stop=%s",
                command.pair_name,
                attempt_index,
                "-" if identity is None else identity.client_order_id or "-",
                command.request.side,
                command.request.ordType,
                _fmt_compact_price(command.request.orderQty),
                _fmt_compact_price(command.request.price),
                _fmt_compact_price(command.request.stopPx),
            )
            return
        if isinstance(command, CancelCommand):
            label = "HEAD_CANCEL_SENT" if command.reason == "head_timeout" else "CANCEL_SENT"
            _LOGGER.info(
                "%s (%s#%s): CID=%s reason=%s",
                label,
                command.pair_name,
                slot.attempt_index,
                command.request.clOrdID,
                command.reason,
            )
            return
        if not isinstance(command, AmendTailCommand):
            return
        pair_state = self.state.pairs.get(command.pair_name)
        if pair_state is None:
            return
        now = datetime.now(timezone.utc)
        if command.request.newPrice is None:
            return
        desired_stop = to_decimal(command.request.newPrice)
        confirmed_stop = _confirmed_tail_stop(pair_state)
        ref_source = "-"
        ref_price: Decimal | None = None
        if self.public_state_reader is not None:
            market = self.public_state_reader.fetch_market_state(
                _pair_symbol(pair_state, self.symbol)
            )
            ref_source, ref_price_value = tail_reference_price(pair_state.pair, market)
            ref_price = to_decimal(ref_price_value)
        _LOGGER.info(
            "AMEND_SENT (%s#%s): CS=%s DS=%s ref=%s src=%s TCID=%s TOID=%s",
            command.pair_name,
            pair_state.attempt_index,
            _fmt_compact_price(confirmed_stop),
            _fmt_compact_price(desired_stop),
            _fmt_compact_price(ref_price),
            ref_source,
            "-" if identity is None else identity.client_order_id or "-",
            "-" if identity is None else identity.exchange_order_id or "-",
        )
        self._pending_tail_amends[slot] = _TailAmendPending(
            pair_name=command.pair_name,
            attempt_index=pair_state.attempt_index,
            desired_stop_price=desired_stop,
            client_order_id=None if identity is None else identity.client_order_id,
            exchange_order_id=None if identity is None else identity.exchange_order_id,
            started_at=now,
            deadline_at=now + timedelta(seconds=self.tail_visibility_timeout_seconds),
        )

    def _log_new_pending_repeats(
        self,
        previous_repeats: Mapping[str, object],
    ) -> None:
        for pair_name, pending in self.chronos.pending_repeats.items():
            if previous_repeats.get(pair_name) == pending:
                continue
            _LOGGER.info(
                "REPEAT_WAIT (%s#%s): ready_at=%s",
                pair_name,
                pending.next_attempt,
                pending.ready_at.isoformat(),
            )

    def _log_chain_releases(self, previous_pairs: Mapping[str, PairCycleState]) -> None:
        for pair_name, current in self.state.pairs.items():
            previous = previous_pairs.get(pair_name)
            if previous is None:
                continue
            token = current.dependency_token
            if token is None or previous.dependency_token == token:
                continue
            _LOGGER.info(
                "CHAIN_READY (%s#%s <- %s#%s): waiting_for_price_gate closed_at=%s",
                pair_name,
                current.attempt_index,
                token.origin_pair_name,
                token.origin_attempt_index,
                token.closed_at.isoformat(),
            )

    def _log_repeat_attempts(self, previous_pairs: Mapping[str, PairCycleState]) -> None:
        for pair_name, current in self.state.pairs.items():
            previous = previous_pairs.get(pair_name)
            if previous is None:
                continue
            if current.attempt_index <= previous.attempt_index:
                continue
            _LOGGER.info(
                "REPEAT_READY (%s#%s): waiting_for_price_gate window=%s..%s baseline=-",
                pair_name,
                current.attempt_index,
                current.pair.window.start_minutes,
                current.pair.window.end_minutes,
            )

    def _log_repeat_start(self, commands: tuple[DragonSong, ...]) -> None:
        started: set[tuple[str, int]] = set()
        for command in commands:
            pair_state = self.state.pairs.get(command.pair_name)
            if pair_state is None:
                continue
            key = (command.pair_name, pair_state.attempt_index)
            if key in started:
                continue
            started.add(key)
            _LOGGER.info(
                "REPEAT_START (%s#%s): commands=%s",
                key[0],
                key[1],
                sum(1 for item in commands if item.pair_name == key[0]),
            )

    def _followup_events(self, command: DragonSong, ack: OrderAck) -> tuple[EggMove, ...]:
        if isinstance(command, AmendTailCommand) and _ack_is_rejected(ack):
            return (
                _tail_amend_event_from_ack(
                    command,
                    ack,
                    symbol=str(command.symbol),
                    kind=EggMoveKind.TAIL_AMEND_REJECTED,
                ),
            )
        if isinstance(command, CancelCommand) and command.reason == "head_timeout":
            timeout_cancel = _head_timeout_cancel_event_from_ack(
                command,
                ack,
                symbol=str(command.symbol),
                pair_state=self.state.pairs.get(command.pair_name),
            )
            if timeout_cancel is not None:
                return (timeout_cancel,)
        if not self.simulate:
            # Live/demo lifecycle is DB-grounded; adapter ACKs are correlation only.
            return ()
        if isinstance(command, PlaceTailCommand):
            submitted_tail = tail_submitted_from_ack(
                pair_name=command.pair_name,
                symbol=str(command.symbol),
                ack=ack,
                client_order_id=command.request.clOrdID,
                stop_price=command.request.stopPx,
                occurred_at=datetime.now(timezone.utc),
            )
            return (submitted_tail,)
        if isinstance(command, AmendTailCommand):
            kind = EggMoveKind.TAIL_AMENDED
            return (
                _tail_amend_event_from_ack(
                    command,
                    ack,
                    symbol=str(command.symbol),
                    kind=kind,
                ),
            )
        if not isinstance(command, PlaceHeadCommand):
            return ()
        submitted = head_submitted_from_ack(
            pair_name=command.pair_name,
            symbol=str(command.symbol),
            ack=ack,
            client_order_id=command.request.clOrdID,
            occurred_at=datetime.now(timezone.utc),
        )
        simulated_played_quantity = _played_quantity_from_request(command.request)
        submitted_with_reference = _with_simulated_reference_price(submitted, command)
        confirmed = simulated_private_fill_from_submission(
            submitted_with_reference,
            played_quantity=simulated_played_quantity,
            closed=True,
        )
        confirmed = _copy_reference_price(confirmed, submitted_with_reference)
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
                pair_name=pair_state.pair.name,
                symbol=_pair_symbol(pair_state, symbol),
                occurred_at=state.launched_at,
            )
            for pair_state in state.pairs.values()
            if pair_dependency_satisfied(state, pair_state)
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


def _pair_symbol_from_pair(pair: object, default_symbol: str) -> str:
    symbol = str(getattr(pair, "symbol", "") or "").strip()
    return symbol or default_symbol


def _pair_symbol(pair_state: PairCycleState, default_symbol: str) -> str:
    return _pair_symbol_from_pair(pair_state.pair, default_symbol)


def _active_runtime_symbols(runtime: RuntimeQueueLike) -> tuple[str, ...]:
    symbols = {
        _pair_symbol(pair_state, runtime.symbol)
        for pair_state in runtime.state.pairs.values()
    }
    if not symbols:
        return (runtime.symbol,)
    return tuple(sorted(symbols))


def _pair_runtime_complete(
    pair_state: PairCycleState,
    *,
    launched_at: datetime,
    now: datetime,
) -> bool:
    """Return true only when head and required tail work have completed."""
    if pair_state.head_state == HeadState.LATENT and pair_window_has_ended(
        pair_state.pair,
        launched_at=launched_at,
        now=now,
    ):
        return True
    if pair_state.head_state == HeadState.FAILED:
        return True
    if pair_state.head_state != HeadState.CLOSED:
        return False
    played = pair_state.played_quantity is not None and pair_state.played_quantity > 0
    if not played:
        return True
    return pair_state.tail_state in {TailState.CLOSED, TailState.FAILED}


def _head_timeout_cancel_command(
    pair_state: PairCycleState,
    symbol: str,
    cancel_id: str,
) -> CancelCommand:
    request = CancelOrderCommandRequest(
        pair_name=pair_state.pair.name,
        clOrdID=cancel_id,
    )
    return CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol(symbol),
        pair_name=pair_state.pair.name,
        request=request,
        reason="head_timeout",
        legacy_order=cast(
            OrderDict,
            {
                "pair_name": pair_state.pair.name,
                "ordType": "cancel",
                "clOrdID": cancel_id,
                "text": "head_timeout",
            },
        ),
    )


def _command_failure_event(
    command: DragonSong,
    *,
    symbol: str,
    occurred_at: datetime,
    error: BaseException,
) -> EggMove | None:
    reply: dict[str, object] = {
        "ordStatus": HeadState.FAILED.value,
        "execType": "command_failed",
        "error": _compact_error(error),
    }
    client_order_id = getattr(command.request, "clOrdID", None)
    if client_order_id is not None:
        reply["clOrdID"] = client_order_id
    order_id = getattr(command.request, "orderID", None)
    if order_id is not None:
        reply["orderID"] = order_id
    price = getattr(command.request, "newPrice", None)
    if price is None:
        price = getattr(command.request, "price", None)
    if price is None:
        price = getattr(command.request, "stopPx", None)
    if price is not None:
        reply["price"] = float(to_decimal(price))
        reply["stopPx"] = float(to_decimal(price))
    quantity = _command_request_quantity(command)
    if quantity is not None:
        reply["orderQty"] = float(quantity)
        reply["cumQty"] = 0.0
    if isinstance(command, PlaceHeadCommand):
        return EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol=symbol,
            pair_name=command.pair_name,
            role=OrderRole.HEAD,
            reply=reply,
        )
    if isinstance(command, PlaceTailCommand):
        return EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=occurred_at,
            symbol=symbol,
            pair_name=command.pair_name,
            role=OrderRole.TAIL,
            reply=reply,
        )
    if isinstance(command, AmendTailCommand):
        return EggMove(
            kind=EggMoveKind.TAIL_AMEND_REJECTED,
            occurred_at=occurred_at,
            symbol=symbol,
            pair_name=command.pair_name,
            role=OrderRole.TAIL,
            reply=reply,
        )
    return None


def _tail_amend_event_from_ack(
    command: AmendTailCommand,
    ack: OrderAck,
    *,
    symbol: str,
    kind: EggMoveKind,
) -> EggMove:
    status = str(ack.status or "")
    reply: dict[str, object] = {
        "orderID": str(ack.order_id),
        "ordStatus": status,
    }
    if command.request.clOrdID is not None:
        reply["clOrdID"] = command.request.clOrdID
    confirmed_price = ack.price if ack.price is not None else command.request.newPrice
    if confirmed_price is not None:
        reply["stopPx"] = float(to_decimal(confirmed_price))
    if ack.orig_qty is not None:
        reply["orderQty"] = float(to_decimal(ack.orig_qty))
    if ack.executed_qty is not None:
        reply["cumQty"] = float(to_decimal(ack.executed_qty))
    if ack.side is not None:
        reply["side"] = ack.side
    return EggMove(
        kind=kind,
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        pair_name=command.pair_name,
        role=OrderRole.TAIL,
        reply=reply,
        is_private=False,
    )


def _head_timeout_cancel_event_from_ack(
    command: CancelCommand,
    ack: OrderAck,
    *,
    symbol: str,
    pair_state: PairCycleState | None,
) -> EggMove | None:
    if not _ack_is_zero_fill_cancel(ack):
        return None
    head_identity = None if pair_state is None else pair_state.head_identity
    client_order_id = (
        None if head_identity is None else head_identity.client_order_id
    )
    exchange_order_id = (
        str(ack.order_id)
        if ack.order_id
        else None if head_identity is None else head_identity.exchange_order_id
    )
    reply: dict[str, object] = {
        "ordStatus": "Canceled",
        "execType": "Canceled",
        "cumQty": 0.0,
    }
    if exchange_order_id:
        reply["orderID"] = exchange_order_id
    else:
        reply["orderID"] = command.request.clOrdID
    if client_order_id:
        reply["clOrdID"] = client_order_id
    if ack.orig_qty is not None:
        reply["orderQty"] = float(to_decimal(ack.orig_qty))
    elif pair_state is not None and pair_state.pair.head_quantity is not None:
        reply["orderQty"] = float(to_decimal(pair_state.pair.head_quantity))
    if ack.price is not None:
        price = float(to_decimal(ack.price))
        reply["price"] = price
        reply["stopPx"] = price
    if ack.side is not None:
        reply["side"] = ack.side
    return EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime.now(timezone.utc),
        symbol=symbol,
        pair_name=command.pair_name,
        role=OrderRole.HEAD,
        reply=reply,
        is_private=False,
    )


def _command_request_quantity(command: DragonSong) -> Decimal | None:
    quantity = getattr(command.request, "orderQty", None)
    if quantity is None:
        quantity = getattr(command.request, "newQty", None)
    if isinstance(quantity, (int, float, Decimal, str)):
        parsed = to_decimal(quantity)
        if parsed >= 0:
            return parsed
    return None


def _with_simulated_reference_price(
    submitted: EggMove,
    command: PlaceHeadCommand,
) -> EggMove:
    """Give relative tails a deterministic reference in simulation mode."""
    reply = dict(submitted.reply or {})
    if "reference_price" not in reply:
        reference = command.request.price or command.request.stopPx or Decimal("100")
        reply["reference_price"] = float(reference)
    return replace(submitted, reply=reply)


def _copy_reference_price(move: EggMove, source: EggMove) -> EggMove:
    source_reply = source.reply or {}
    reference = source_reply.get("reference_price", source_reply.get("price"))
    if reference is None:
        return move
    reply = dict(move.reply or {})
    reply["reference_price"] = reference
    return replace(move, reply=reply)


def _played_quantity_from_ack(ack: OrderAck) -> Decimal | None:
    if ack.executed_qty is None:
        return None
    return Decimal(str(ack.executed_qty))


def _ack_is_terminal(ack: OrderAck) -> bool:
    return ack.status.replace(" ", "_").replace("-", "_").lower() in {
        "filled",
        "closed",
        "fully_filled",
        "full_fill",
    }


def _ack_is_rejected(ack: OrderAck) -> bool:
    return ack.status.replace(" ", "").replace("_", "").replace("-", "").lower() in {
        "rejected",
        "reject",
        "failed",
        "invalidprice",
    }


def _ack_is_zero_fill_cancel(ack: OrderAck) -> bool:
    status = ack.status.replace(" ", "").replace("_", "").replace("-", "").lower()
    if ack.executed_qty is None:
        return status in {"canceled", "cancelled"}
    if status not in {"canceled", "cancelled", "notfound", "notfoundcancelled"}:
        return False
    return to_decimal(ack.executed_qty) == Decimal("0")


def _record_matches_identity(record, identity: OrderIdentity) -> bool:
    record_symbol = _record_symbol(record)
    if identity.symbol is not None:
        if record_symbol is None or record_symbol != identity.symbol:
            return False
    client_order_id = getattr(record, "client_order_id", None)
    exchange_order_id = getattr(record, "exchange_order_id", None)
    if client_order_id and identity.client_order_id == client_order_id:
        return True
    if (
        exchange_order_id
        and identity.exchange_order_id == exchange_order_id
    ):
        return True
    return False


def _record_symbol(record: object) -> str | None:
    symbol = getattr(record, "symbol", None)
    if symbol is None:
        return None
    text = str(symbol).strip()
    return text or None


def _identity_key(identity: OrderIdentity) -> str:
    return (
        f"{identity.symbol or '-'}|{identity.pair_name}|{identity.role}|"
        f"{identity.client_order_id or '-'}|{identity.exchange_order_id or '-'}"
    )


def _identity_confirmed(expected: OrderIdentity, current: OrderIdentity | None) -> bool:
    if current is None:
        return False
    return _identities_overlap(
        current,
        expected.client_order_id,
        expected.exchange_order_id,
    )


def _identities_overlap(
    identity: OrderIdentity,
    client_order_id: str | None,
    exchange_order_id: str | None,
) -> bool:
    if client_order_id and identity.client_order_id == client_order_id:
        return True
    if exchange_order_id and identity.exchange_order_id == exchange_order_id:
        return True
    return False


def _role_from_move(move: EggMove) -> OrderRole:
    return OrderRole.HEAD if move.role == OrderRole.HEAD else OrderRole.TAIL


def _confirmed_tail_stop(pair_state: PairCycleState) -> Decimal | None:
    if pair_state.tail_trail is None:
        return None
    return pair_state.tail_trail.confirmed_stop_price


def _tail_signed_distance(
    pair_state: PairCycleState,
    reference: Decimal,
    stop: Decimal,
) -> Decimal:
    """Return positive distance only while the platform stop is live-side safe."""
    if pair_state.pair.tail.side.value.lower() == "sell":
        return reference - stop
    return stop - reference


def _pair_uses_relative_tail(pair_state: PairCycleState) -> bool:
    tail_type = (pair_state.pair.tail_price_spec_type or "").lower()
    amount_type = pair_state.pair.amount_type.lower()
    return "t%" in tail_type or "td" in tail_type or "t%" in amount_type or "td" in amount_type


def _runtime_sources_should_stop(runtime: RuntimeQueueLike) -> bool:
    keep_alive = bool(getattr(runtime, "should_keep_sources_alive", False))
    return runtime.all_pairs_terminal and not keep_alive


def _played_quantity_from_move(move: EggMove) -> Decimal | None:
    payload = move.reply or {}
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            return to_decimal(value)
    return None


def _filled_quantity_from_move_payload(payload: Mapping[str, object]) -> Decimal | None:
    for key in ("cumQty", "executedQty", "filledQty", "filled_quantity"):
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            return to_decimal(value)
    return None


def _head_fill_price_from_move_payload(payload: Mapping[str, object]) -> Decimal | None:
    for key in ("price", "avgPx", "lastPx", "fillPrice", "executed_price"):
        value = payload.get(key)
        if isinstance(value, (int, float, Decimal, str)):
            return to_decimal(value)
    return None


def _fmt_compact_price(value: float | Decimal | None) -> str:
    if value is None:
        return "-"
    as_float = float(value)
    decimals = 2 if abs(as_float) >= 1 else 4
    return f"{as_float:.{decimals}f}"


def _event_atom(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else "-"
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def _compact_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())


def _prices_close(
    left: Decimal | None,
    right: Decimal | None,
    *,
    epsilon: Decimal = Decimal("0.00000001"),
) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= epsilon


def _record_timestamp(record) -> datetime | None:
    if record.local_timestamp is not None:
        return _as_utc_aware(datetime.fromisoformat(record.local_timestamp))
    if record.source_timestamp is not None:
        return _as_utc_aware(datetime.fromisoformat(record.source_timestamp))
    return None


def _as_utc_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
