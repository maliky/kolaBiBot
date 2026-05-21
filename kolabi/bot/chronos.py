"""Async strategy supervisor shell around the pure Isis reducer.

Purpose: own persistent strategy cache, apply event ordering and deduplication,
and forward typed runtime commands to the execution layer.
Inputs: typed `EggMove` values from market/private/account listeners.
Outputs: typed `RuntimeCommand` values and supervisor notices.
Side effects: async queue IO only.
Important types: `StrategyState`, `EggMove`, `RuntimeCommand`,
`ChronosNotice`.
Role: interpreter shell.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Iterable

from kolabi.bot.domain import EggMove, EggMoveKind, HeadState, StrategyState, TailState
from kolabi.bot.isis import resolve_pair_name, step_strategy
from kolabi.shared.core.runtime_types import RuntimeCommand, RuntimeCommandKind


class ChronosNoticeKind(StrEnum):
    DUPLICATE_EVENT_IGNORED = "DuplicateEventIgnored"
    PUBLIC_EVENT_IGNORED = "PublicEventIgnoredByPrivateTerminal"
    PENDING_IDENTITY_TIMEOUT = "PendingIdentityTimeout"


@dataclass(frozen=True)
class ChronosNotice:
    kind: ChronosNoticeKind
    pair_name: str | None
    event_id: str | None
    noted_at: datetime
    detail: str | None = None


@dataclass(frozen=True)
class PendingEggMove:
    event: EggMove
    first_seen_at: datetime


@dataclass
class Chronos:
    """Supervise typed strategy events and forward typed runtime commands."""

    state: StrategyState
    pending_timeout: timedelta = timedelta(seconds=30)
    event_queue: asyncio.Queue[EggMove | None] = field(default_factory=asyncio.Queue)
    command_queue: asyncio.Queue[RuntimeCommand] = field(default_factory=asyncio.Queue)
    notices: list[ChronosNotice] = field(default_factory=list)
    pending: dict[str, PendingEggMove] = field(default_factory=dict)
    _seen_event_keys: set[tuple[str, str]] = field(default_factory=set)
    _seen_fallback_keys: set[tuple[str, str, int]] = field(default_factory=set)
    _seen_command_keys: set[tuple[str, str, str | None]] = field(default_factory=set)

    async def run_once(self) -> tuple[RuntimeCommand, ...]:
        """Traite le lot courant d'evenements deja en file."""
        batch = await self._drain_batch()
        commands = self.process_events(batch)
        for command in commands:
            await self.command_queue.put(command)
        return commands

    def process_events(
        self,
        events: Iterable[EggMove],
        *,
        now: datetime | None = None,
    ) -> tuple[RuntimeCommand, ...]:
        """Applique precedence et routage sur un lot d'evenements."""
        current_time = now or datetime.now(timezone.utc)
        selected: list[EggMove] = []
        private_terminal_pairs: dict[str, EggMove] = {}
        public_ignored: list[EggMove] = []

        for event in events:
            pair_name = resolve_pair_name(self.state, event) or event.pair_name
            if pair_name and _is_private_terminal(event):
                private_terminal_pairs[pair_name] = event
                continue
            selected.append(event)

        if private_terminal_pairs:
            filtered: list[EggMove] = []
            for event in selected:
                pair_name = resolve_pair_name(self.state, event) or event.pair_name
                if pair_name and pair_name in private_terminal_pairs and not event.is_private:
                    public_ignored.append(event)
                    continue
                filtered.append(event)
            selected = filtered + list(private_terminal_pairs.values())

        for event in public_ignored:
            self.notices.append(
                ChronosNotice(
                    kind=ChronosNoticeKind.PUBLIC_EVENT_IGNORED,
                    pair_name=resolve_pair_name(self.state, event) or event.pair_name,
                    event_id=event.event_id,
                    noted_at=current_time,
                    detail="private terminal event has precedence",
                )
            )

        emitted: list[RuntimeCommand] = []
        for event in selected:
            emitted.extend(self.process_event(event, now=current_time))
        return self._dedupe_commands(emitted)

    def process_event(
        self,
        event: EggMove,
        *,
        now: datetime | None = None,
    ) -> tuple[RuntimeCommand, ...]:
        """Traite un evenement unique avec deduplication et attente d'identite."""
        current_time = now or datetime.now(timezone.utc)
        if self._needs_pending_identity(event):
            pending_key = event.event_id or f"pending:{id(event)}"
            self.pending[pending_key] = PendingEggMove(event=event, first_seen_at=current_time)
            return ()

        pair_name = resolve_pair_name(self.state, event) or event.pair_name
        event_key = self._event_key(pair_name, event)
        if event_key is not None:
            if len(event_key) == 2:
                key = (event_key[0], event_key[1])
                if key in self._seen_event_keys:
                    self._record_duplicate(pair_name, event, current_time)
                    return ()
                self._seen_event_keys.add(key)
            else:
                fallback_key = (event_key[0], event_key[1], int(event_key[2]))
                if fallback_key in self._seen_fallback_keys:
                    self._record_duplicate(pair_name, event, current_time)
                    return ()
                self._seen_fallback_keys.add(fallback_key)

        self.state, commands = step_strategy(self.state, event)
        chained_commands = self._activate_dependent_pairs(event)
        return tuple(commands) + chained_commands

    def expire_pending(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[ChronosNotice, ...]:
        """Expire les evenements sans identite suffisante apres delai."""
        current_time = now or datetime.now(timezone.utc)
        expired: list[str] = []
        notices: list[ChronosNotice] = []
        for key, pending_event in self.pending.items():
            if current_time - pending_event.first_seen_at < self.pending_timeout:
                continue
            notices.append(
                ChronosNotice(
                    kind=ChronosNoticeKind.PENDING_IDENTITY_TIMEOUT,
                    pair_name=pending_event.event.pair_name,
                    event_id=pending_event.event.event_id,
                    noted_at=current_time,
                    detail="missing identity prevents confirmation match",
                )
            )
            expired.append(key)
        for key in expired:
            self.pending.pop(key, None)
        self.notices.extend(notices)
        return tuple(notices)

    async def _drain_batch(self) -> list[EggMove]:
        first = await self.event_queue.get()
        if first is None:
            return []
        batch = [first]
        while True:
            try:
                next_item = self.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if next_item is None:
                break
            batch.append(next_item)
        return batch

    def _record_duplicate(
        self,
        pair_name: str | None,
        event: EggMove,
        current_time: datetime,
    ) -> None:
        self.notices.append(
            ChronosNotice(
                kind=ChronosNoticeKind.DUPLICATE_EVENT_IGNORED,
                pair_name=pair_name,
                event_id=event.event_id,
                noted_at=current_time,
            )
        )

    def _needs_pending_identity(self, event: EggMove) -> bool:
        if not event.is_private:
            return False
        if resolve_pair_name(self.state, event) or event.pair_name:
            return False
        if event.event_id:
            return False
        return _event_order_id(event) is None

    def _event_key(
        self,
        pair_name: str | None,
        event: EggMove,
    ) -> tuple[str, str] | tuple[str, str, float] | None:
        if pair_name is None:
            return None
        if event.event_id:
            return pair_name, event.event_id
        order_id = _event_order_id(event)
        status = _event_status(event)
        if order_id is None or status is None:
            return None
        ts_bucket = event.occurred_at.timestamp() // 1
        return pair_name, f"{order_id}:{status}", ts_bucket

    def _dedupe_commands(
        self,
        commands: Iterable[RuntimeCommand],
    ) -> tuple[RuntimeCommand, ...]:
        per_pair: dict[str, RuntimeCommand] = {}
        for command in commands:
            if not isinstance(command, RuntimeCommand):
                raise TypeError("Chronos forwards typed RuntimeCommand values only")
            pair_name = _command_pair_name(command)
            if pair_name is None:
                continue
            command_key = (pair_name, f"{command.kind}:{command.reason}", _command_client_order_id(command))
            if command_key in self._seen_command_keys:
                continue
            self._seen_command_keys.add(command_key)
            previous = per_pair.get(pair_name)
            if previous is None or _command_precedence(command) >= _command_precedence(previous):
                per_pair[pair_name] = command
        return tuple(per_pair.values())

    def _activate_dependent_pairs(self, event: EggMove) -> tuple[RuntimeCommand, ...]:
        """Active les paires dependantes apres une fermeture significative."""
        origin_pair = resolve_pair_name(self.state, event) or event.pair_name
        if origin_pair is None:
            return ()
        if not _may_activate_dependency(self.state, origin_pair, event):
            return ()

        emitted: list[RuntimeCommand] = []
        for pair_name, pair_state in self.state.pairs.items():
            if pair_name == origin_pair:
                continue
            hook_name = pair_state.pair.hook_name
            if hook_name not in {f"{origin_pair}-tail-closed", f"{origin_pair}-closed"}:
                continue
            if pair_state.head_state != HeadState.LATENT:
                continue
            synthetic_event = EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=event.occurred_at,
                symbol=event.symbol,
                event_id=None if event.event_id is None else f"{event.event_id}:hook:{pair_name}",
                pair_name=pair_name,
            )
            self.state, commands = step_strategy(self.state, synthetic_event)
            emitted.extend(commands)
        return self._dedupe_commands(emitted)


def _command_pair_name(command: RuntimeCommand) -> str | None:
    if command.order is None:
        return None
    pair_name = command.order.get("pair_name")
    return pair_name if isinstance(pair_name, str) and pair_name else None


def _command_client_order_id(command: RuntimeCommand) -> str | None:
    if command.order is None:
        return None
    candidate = command.order.get("clOrdID")
    return candidate if isinstance(candidate, str) and candidate else None


def _command_precedence(command: RuntimeCommand) -> int:
    if command.kind == RuntimeCommandKind.CANCEL:
        return 30
    if command.kind == RuntimeCommandKind.AMEND:
        return 20
    if command.kind == RuntimeCommandKind.PLACE:
        return 10
    return 0


def _event_order_id(event: EggMove) -> str | None:
    for payload in (event.order, event.reply):
        if payload is None:
            continue
        for field in ("orderID", "clOrdID"):
            candidate = payload.get(field)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _event_status(event: EggMove) -> str | None:
    for payload in (event.reply, event.order):
        if payload is None:
            continue
        for field in ("ordStatus", "status"):
            candidate = payload.get(field)
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def _is_private_terminal(event: EggMove) -> bool:
    return event.is_private and event.kind in {
        EggMoveKind.NOT_PLAYED_CANCELED,
        EggMoveKind.PLAYED_AND_CANCELED,
    }


def _may_activate_dependency(
    state: StrategyState,
    origin_pair: str,
    event: EggMove,
) -> bool:
    if not event.is_private:
        return False
    pair_state = state.pairs.get(origin_pair)
    if pair_state is None:
        return False
    if pair_state.tail_state == TailState.CLOSED:
        return True
    return pair_state.head_state == HeadState.CLOSED
