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
from dataclasses import dataclass, replace
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import count
from typing import Mapping, Protocol, assert_never, cast

from kolabi.bot.chronos import Chronos, ChronosNotice, resolve_pair_name
from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadState,
    OrderRole,
    OrderIdentity,
    PairCycleState,
    StrategySpec,
    StrategyState,
    TailState,
)
from kolabi.bot.ids import head_client_order_id, tail_client_order_id
from kolabi.bot.persistence import TailTelemetryRow
from kolabi.bot.order_building import head_order_dict
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
from kolabi.bot.pricing import tail_reference_price
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import (
    AmendHeadCommand,
    CancelCommand,
    DragonSong,
    AmendTailCommand,
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
        poll_seconds: float = 0.25,
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
                last_price=getattr(market, "last_price", None),
                mark_price=getattr(market, "mark_price", None),
                index_price=getattr(market, "index_price", None),
                tick_size=getattr(market, "tick_size", None),
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
                reference_key = ""
                if event_prefix == "public-market" and move.reply is not None:
                    reference_key = f":{move.reply.get('reference_source', '')}:{move.reply.get('reference_price', '')}"
                event_id = (
                    f"{event_prefix}:{pair_name}:"
                    f"{market.recorded_at or snapshot.occurred_at.isoformat()}{reference_key}"
                )
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
        poll_seconds: float = 0.25,
        head_fill_reference_grace_seconds: float = _DEFAULT_HEAD_FILL_REFERENCE_GRACE_SECONDS,
    ) -> None:
        self.client = client
        self.poll_seconds = poll_seconds
        self.head_fill_reference_grace_seconds = max(
            0.0, head_fill_reference_grace_seconds
        )
        self.after_local_timestamp: datetime | None = None
        self.after_local_id: int | None = None
        self.after_fill_timestamp: datetime | None = None
        self.after_fill_id: int | None = None
        self._pending_records: list[_PendingPrivateRecord] = []
        self._cursor_initialised = False

    async def pump(self, runtime: RuntimeQueueLike) -> None:
        while runtime.running:
            now = datetime.now(timezone.utc)
            if not self._cursor_initialised:
                self.after_local_timestamp = runtime.state.launched_at - timedelta(seconds=5)
                self.after_local_id = None
                self.after_fill_timestamp = runtime.state.launched_at - timedelta(seconds=5)
                self.after_fill_id = None
                self._cursor_initialised = True
            records = self.client.fetch_private_orders_since(
                after_local_timestamp=self.after_local_timestamp,
                after_local_id=self.after_local_id,
                symbol=runtime.symbol,
            )
            fill_records = self.client.fetch_private_fills_since(
                after_local_timestamp=self.after_fill_timestamp,
                after_local_id=self.after_fill_id,
                symbol=runtime.symbol,
            )
            active_client_ids, active_exchange_ids = self._active_identity_sets(runtime)
            if active_client_ids or active_exchange_ids:
                fetch_orders_by_identity = getattr(
                    self.client, "fetch_private_orders_for_identities", None
                )
                if callable(fetch_orders_by_identity):
                    identity_orders = fetch_orders_by_identity(
                        client_order_ids=active_client_ids,
                        exchange_order_ids=active_exchange_ids,
                        symbol=runtime.symbol,
                    )
                    records = self._merge_unique_private_records(records, identity_orders)
                fetch_fills_by_identity = getattr(
                    self.client, "fetch_private_fills_for_identities", None
                )
                if callable(fetch_fills_by_identity):
                    identity_fills = fetch_fills_by_identity(
                        client_order_ids=active_client_ids,
                        exchange_order_ids=active_exchange_ids,
                        symbol=runtime.symbol,
                    )
                    fill_records = self._merge_unique_private_records(
                        fill_records, identity_fills
                    )
            candidates = tuple(self._pending_records)
            self._pending_records = []
            candidates = candidates + tuple(
                _PendingPrivateRecord(record=record, first_seen_at=now) for record in records
            ) + tuple(
                _PendingPrivateRecord(record=record, first_seen_at=now)
                for record in fill_records
            )
            for pending_record in candidates:
                record = pending_record.record
                occurred_at = _record_timestamp(record)
                is_new_record = record in records
                is_new_fill = record in fill_records
                if is_new_record:
                    if occurred_at is not None:
                        self.after_local_timestamp = occurred_at
                    self.after_local_id = record.local_id
                if is_new_fill:
                    if occurred_at is not None:
                        self.after_fill_timestamp = occurred_at
                    self.after_fill_id = record.local_id
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
                move = self._with_reference_price(move, pair_state, runtime.symbol)
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
                    (
                        f"private-fill:{record.local_id}"
                        if is_new_fill and record.local_id is not None
                        else (
                            f"private-order:{record.local_id}"
                            if record.local_id is not None
                            else None
                        )
                    )
                )
                await runtime.enqueue(replace(move, event_id=event_id))
            if runtime.all_pairs_terminal:
                return
            await asyncio.sleep(self.poll_seconds)

    @staticmethod
    def _active_identity_sets(runtime: RuntimeQueueLike) -> tuple[tuple[str, ...], tuple[str, ...]]:
        client_ids: set[str] = set()
        exchange_ids: set[str] = set()
        for pair_state in runtime.state.pairs.values():
            for identity in (pair_state.head_identity, pair_state.tail_identity):
                if identity is None:
                    continue
                if identity.client_order_id:
                    client_ids.add(identity.client_order_id)
                if identity.exchange_order_id:
                    exchange_ids.add(identity.exchange_order_id)
        if isinstance(runtime, StrategyRuntime):
            for identity in runtime._command_identities().values():
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
        self._command_errors: list[BaseException] = []
        self._legend_logged = False
        self._last_pair_updates: dict[str, tuple[str, ...]] = {}
        self._last_tail_metrics: dict[str, tuple[str, ...]] = {}
        self._head_order_lifecycle: dict[str, _OrderLifecycleSnapshot] = {}
        self._live_command_identities: dict[str, OrderIdentity] = {}
        self._pending_tail_visibility: dict[_CommandSlot, _TailVisibilityWindow] = {}

    def _pair_state(self, pair):
        from kolabi.bot.domain import PairCycleState

        return PairCycleState(pair=pair)

    @property
    def all_pairs_terminal(self) -> bool:
        return all(_pair_runtime_complete(pair_state) for pair_state in self.state.pairs.values())

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
        self._pending_tail_visibility.clear()

    async def run(self) -> StrategyRunResult:
        await self.start()
        try:
            while self.running:
                current_time = datetime.now(timezone.utc)
                self.chronos.expire_pending(now=current_time)
                self._reap_command_tasks()
                if self._command_errors:
                    raise self._command_errors[0]
                self._prune_live_command_identities()
                self._check_tail_visibility_deadlines(current_time)
                repeat_commands = self.chronos.activate_ready_repeats(
                    symbol=self.symbol,
                    now=current_time,
                )
                if repeat_commands:
                    previous_pairs = dict(self.state.pairs)
                    self.state = self.chronos.state
                    self._dispatch_commands(repeat_commands)
                    self._log_living_updates(previous_pairs)
                if (
                    self.all_pairs_terminal
                    and self.event_queue.empty()
                    and not self._inflight_commands
                    and not self._pending_commands
                ):
                    break
                try:
                    event = await asyncio.wait_for(self.event_queue.get(), timeout=0.2)
                except TimeoutError:
                    continue
                previous_pairs = dict(self.state.pairs)
                self._record_head_lifecycle(event)
                commands = self.chronos.process_event(event)
                self.state = self.chronos.state
                self._dispatch_commands(commands)
                self._log_living_updates(previous_pairs)
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
        self._inflight_commands[slot] = _InFlightCommand(
            command=prepared,
            task=asyncio.create_task(self._execute_and_enqueue(prepared)),
        )

    async def _execute_and_enqueue(self, prepared: DragonSong) -> None:
        if self.executor is None:
            return
        ack = await self.executor.execute(prepared)
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
            market = (
                None
                if self.public_state_reader is None
                else self.public_state_reader.fetch_market_state(self.symbol)
            )
            rows = self._collect_tail_telemetry_rows(now)
            if rows and self.tail_telemetry_writer is not None:
                self.tail_telemetry_writer.record_rows(rows)
            for row in rows:
                source = "unknown"
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
                    _fmt_compact_price(None if market is None else getattr(market, "last_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "mark_price", None)),
                    _fmt_compact_price(None if market is None else getattr(market, "index_price", None)),
                )
                if self._last_tail_metrics.get(row.pair_name) == signature:
                    continue
                self._last_tail_metrics[row.pair_name] = signature
                _LOGGER.info(
                    "METRICS (%s#%s): (%s--%s) ref=%s stop=%s ID=%s CD=%s LU=%s src=%s px=L:%s M:%s I:%s",
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
                )
            await asyncio.sleep(interval)

    def _collect_tail_telemetry_rows(self, now: datetime) -> tuple[TailTelemetryRow, ...]:
        reader = self.public_state_reader
        if reader is None:
            return ()
        rows: list[TailTelemetryRow] = []
        market = reader.fetch_market_state(self.symbol)
        for pair_name, pair_state in self.state.pairs.items():
            if (
                pair_state.tail_trail is None
                or pair_state.tail_state not in {TailState.HOOKED, TailState.SUBMITTED, TailState.LIVING}
            ):
                continue
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
                    symbol=self.symbol,
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
            if current.head_state not in {HeadState.LIVING, HeadState.CLOSED} and current.tail_state not in {
                TailState.LIVING,
                TailState.SUBMITTED,
            }:
                continue
            quantity_changed = current.played_quantity != previous.played_quantity
            stop_previous = _confirmed_tail_stop(previous)
            stop_current = _confirmed_tail_stop(current)
            desired_stop = (
                None if current.tail_trail is None else current.tail_trail.current_stop_price
            )
            stop_changed = stop_current != stop_previous
            state_changed = (
                current.head_state != previous.head_state
                or current.tail_state != previous.tail_state
            )
            if not (quantity_changed or stop_changed or state_changed):
                continue
            head_lifecycle = self._head_order_lifecycle.get(pair_name, _OrderLifecycleSnapshot())
            update_signature = (
                str(current.attempt_index),
                current.head_state.value,
                current.tail_state.value if current.tail_state is not None else "-",
                str(current.played_quantity) if current.played_quantity is not None else "-",
                _fmt_compact_price(stop_current),
                _fmt_compact_price(desired_stop),
                head_lifecycle.side or "-",
                _fmt_compact_price(head_lifecycle.filled_qty),
                _fmt_compact_price(head_lifecycle.filled_price),
                head_lifecycle.filled_at.isoformat() if head_lifecycle.filled_at is not None else "-",
            )
            if self._last_pair_updates.get(pair_name) == update_signature:
                continue
            self._last_pair_updates[pair_name] = update_signature
            _LOGGER.info(
                "UPDATE (%s#%s): (%s--%s) PQ=%s CS=%s DS=%s HFS=%s HFQ=%s HFP=%s HFT=%s",
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

    def _log_runtime_legend_once(self) -> None:
        if self._legend_logged:
            return
        self._legend_logged = True
        _LOGGER.info(
            "RAPPEL: AI=attempt_index PU=pair_update HS=head_state TS=tail_state PQ=played_qty CS=confirmed_stop DS=desired_stop ID=initial_dist CD=current_dist LU=last_update HFS=head_fill_side HFQ=head_fill_qty HFP=head_fill_price HFT=head_fill_time"
        )

    def _record_head_lifecycle(self, move: EggMove) -> None:
        if move.role != OrderRole.HEAD or move.reply is None:
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
                side_value = pair_state.pair.head.side.value
        previous = self._head_order_lifecycle.get(pair_name, _OrderLifecycleSnapshot())
        self._head_order_lifecycle[pair_name] = _OrderLifecycleSnapshot(
            side=side_value,
            filled_qty=filled_qty if filled_qty is not None else previous.filled_qty,
            filled_price=filled_price if filled_price is not None else previous.filled_price,
            filled_at=move.occurred_at if (filled_qty is not None or filled_price is not None) else previous.filled_at,
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
                # cancel correlation can be dropped once the tail is terminal.
                if pair_state.tail_state in {None, TailState.CLOSED, TailState.FAILED}:
                    stale.append(key)
        for key in stale:
            self._live_command_identities.pop(key, None)

    def pair_state_for_record(self, record) -> tuple[PairCycleState, OrderRole] | None:
        for identity in self._identity_map_from_pair_and_commands().values():
            if identity.role not in {"head", "tail"}:
                continue
            pair_state = self.state.pairs.get(identity.pair_name)
            if pair_state is None:
                continue
            if _record_matches_identity(record, identity):
                role = OrderRole.HEAD if identity.role == "head" else OrderRole.TAIL
                return pair_state, role
        for pair_state in self.state.pairs.values():
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
            return replace(
                command,
                request=request,
                legacy_order=head_order_dict(pair, client_order_id=clordid),
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
        )
        self._live_command_identities[_identity_key(merged)] = merged
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

    def _check_tail_visibility_deadlines(self, now: datetime) -> None:
        stale_slots: list[_CommandSlot] = []
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
                stale_slots.append(slot)
                continue
            if pair_state.tail_state in {TailState.CLOSED, TailState.FAILED}:
                stale_slots.append(slot)
                continue
            if now < window.deadline_at:
                continue
            raise RuntimeError(
                "tail order not visible in private DB before deadline: "
                f"pair={window.pair_name} attempt={window.attempt_index} "
                f"clOrdID={window.client_order_id or '-'} "
                f"orderID={window.exchange_order_id or '-'} "
                f"timeout={self.tail_visibility_timeout_seconds:.1f}s"
            )
        for slot in stale_slots:
            self._pending_tail_visibility.pop(slot, None)

    def _followup_events(self, command: DragonSong, ack: OrderAck) -> tuple[EggMove, ...]:
        if not self.simulate:
            # Live/demo lifecycle is DB-grounded; adapter ACKs are correlation only.
            return ()
        if isinstance(command, PlaceTailCommand):
            submitted_tail = tail_submitted_from_ack(
                pair_name=command.pair_name,
                symbol=self.symbol,
                ack=ack,
                client_order_id=command.request.clOrdID,
                stop_price=command.request.stopPx,
                occurred_at=datetime.now(timezone.utc),
            )
            return (submitted_tail,)
        if isinstance(command, AmendTailCommand):
            now = datetime.now(timezone.utc)
            status = str(ack.status or "")
            kind = (
                EggMoveKind.TAIL_AMEND_REJECTED
                if status.replace(" ", "").replace("_", "").replace("-", "").lower()
                in {"rejected", "reject", "failed", "invalidprice"}
                else EggMoveKind.TAIL_AMENDED
            )
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
            return (
                EggMove(
                    kind=kind,
                    occurred_at=now,
                    symbol=self.symbol,
                    pair_name=command.pair_name,
                    role=OrderRole.TAIL,
                    reply=reply,
                    is_private=False,
                ),
            )
        if not isinstance(command, PlaceHeadCommand):
            return ()
        submitted = head_submitted_from_ack(
            pair_name=command.pair_name,
            symbol=self.symbol,
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


def _pair_runtime_complete(pair_state: PairCycleState) -> bool:
    """Return true only when head and required tail work have completed."""
    if pair_state.head_state == HeadState.FAILED:
        return True
    if pair_state.head_state != HeadState.CLOSED:
        return False
    played = pair_state.played_quantity is not None and pair_state.played_quantity > 0
    if not played:
        return True
    return pair_state.tail_state in {TailState.CLOSED, TailState.FAILED}


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


def _record_matches_identity(record, identity: OrderIdentity) -> bool:
    if record.client_order_id and identity.client_order_id == record.client_order_id:
        return True
    if (
        record.exchange_order_id
        and identity.exchange_order_id == record.exchange_order_id
    ):
        return True
    return False


def _identity_key(identity: OrderIdentity) -> str:
    return (
        f"{identity.pair_name}|{identity.role}|"
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
