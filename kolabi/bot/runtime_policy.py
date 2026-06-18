"""Pure runtime policy helpers for the async strategy shell.

Purpose: keep small command-scheduling decisions deterministic and testable
without touching queues, tasks, exchange clients, telemetry, or logs.
Inputs: immutable strategy state, command values, command slots, and time.
Outputs: derived command slots, active-pair sets, capacity decisions, and
pending-command queues.
Side effects: none.
Important types: `CommandSlot`, `StrategyState`, `DragonSong`.
Role: pure logic.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from kolabi.bot.domain import HeadState, PairCycleState, StrategyState, TailState
from kolabi.bot.pricing import pair_window_has_ended
from kolabi.shared.core.runtime_types import (
    AmendTailCommand,
    CancelCommand,
    DragonSong,
    PlaceHeadCommand,
    PlaceTailCommand,
)


@dataclass(frozen=True)
class CommandSlot:
    pair_name: str
    attempt_index: int
    role: str


def command_slot(command: DragonSong, *, state: StrategyState) -> CommandSlot:
    pair_state = state.pairs.get(command.pair_name)
    role = "head"
    if isinstance(command, (PlaceTailCommand, AmendTailCommand)):
        role = "tail"
    elif isinstance(command, CancelCommand):
        role = "cancel"
    return CommandSlot(
        pair_name=command.pair_name,
        attempt_index=1 if pair_state is None else pair_state.attempt_index,
        role=role,
    )


def command_slot_still_live(slot: CommandSlot, *, state: StrategyState) -> bool:
    pair_state = state.pairs.get(slot.pair_name)
    if pair_state is None:
        return False
    if pair_state.attempt_index != slot.attempt_index:
        return False
    if slot.role == "tail":
        return pair_state.tail_state not in {None, TailState.CLOSED, TailState.FAILED}
    if slot.role == "head":
        return pair_state.head_state not in {HeadState.CLOSED, HeadState.FAILED}
    return True


def active_pair_names(
    state: StrategyState,
    *,
    inflight_commands: Iterable[tuple[CommandSlot, DragonSong]] = (),
    now: datetime,
) -> frozenset[str]:
    active: set[str] = set()
    for pair_name, pair_state in state.pairs.items():
        if pair_state.head_state == HeadState.LATENT:
            continue
        if pair_runtime_complete(
            pair_state,
            launched_at=state.launched_at,
            now=now,
        ):
            continue
        active.add(pair_name)
    for slot, command in inflight_commands:
        if isinstance(command, (PlaceHeadCommand, PlaceTailCommand, AmendTailCommand)):
            active.add(slot.pair_name)
    return frozenset(active)


def active_pair_count(
    state: StrategyState,
    *,
    inflight_commands: Iterable[tuple[CommandSlot, DragonSong]] = (),
    now: datetime,
) -> int:
    return len(
        active_pair_names(
            state,
            inflight_commands=inflight_commands,
            now=now,
        )
    )


def head_capacity_available(
    command: PlaceHeadCommand,
    *,
    state: StrategyState,
    max_active_pairs: int,
    inflight_commands: Iterable[tuple[CommandSlot, DragonSong]] = (),
    now: datetime,
) -> bool:
    if max_active_pairs <= 0:
        return True
    active_pairs = active_pair_names(
        state,
        inflight_commands=inflight_commands,
        now=now,
    )
    if command.pair_name in active_pairs:
        return True
    return len(active_pairs) < max_active_pairs


def append_pending_command(
    pending: Iterable[DragonSong],
    command: DragonSong,
) -> deque[DragonSong]:
    if isinstance(command, AmendTailCommand):
        next_pending = deque(
            queued for queued in pending if not isinstance(queued, AmendTailCommand)
        )
    else:
        next_pending = deque(pending)
    next_pending.append(command)
    return next_pending


def pair_runtime_complete(
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
