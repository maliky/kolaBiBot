"""Pure strategy supervisor reducer built above the pair reducer.

Purpose: route one typed event to the targeted pair, delegate lifecycle
semantics to `step_pair`, and return typed runtime commands without side
effects.
Inputs: immutable `StrategyState` and one `EggMove`.
Outputs: updated `StrategyState` and typed `RuntimeCommand` values.
Side effects: none.
Important types: `StrategyState`, `PairCycleState`, `EggMove`,
`RuntimeCommand`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import replace
from typing import cast

from kolabi.bot.domain import EggMove, PairCycleState, StrategyState
from kolabi.bot.pair_cycle import intents_to_commands, step_pair
from kolabi.shared.core.runtime_types import RuntimeCommand, Symbol


def step_strategy(
    state: StrategyState,
    event: EggMove,
) -> tuple[StrategyState, tuple[RuntimeCommand, ...]]:
    """Route un evenement vers une paire et retourne les commandes emises."""
    pair_name = resolve_pair_name(state, event)
    if pair_name is None:
        next_state = replace(
            state,
            last_event_id=event.event_id,
            last_event_ts=event.occurred_at,
        )
        return next_state, ()

    pair_state = state.pairs.get(pair_name)
    if pair_state is None:
        next_state = replace(
            state,
            last_event_id=event.event_id,
            last_event_ts=event.occurred_at,
        )
        return next_state, ()

    next_pair_state, intents = step_pair(pair_state, event)
    commands = intents_to_commands(
        next_pair_state,
        intents,
        symbol=cast(Symbol, event.symbol),
    )
    next_pair_state = _pair_state_with_supervisor_metadata(
        next_pair_state,
        event=event,
        commands=commands,
    )
    next_state = replace(
        state,
        pairs={**state.pairs, pair_name: next_pair_state},
        last_event_id=event.event_id,
        last_event_ts=event.occurred_at,
    )
    return next_state, commands


def resolve_pair_name(state: StrategyState, event: EggMove) -> str | None:
    """Resout la paire cible via le nom explicite puis les identites d'ordre."""
    if event.pair_name and event.pair_name in state.pairs:
        return event.pair_name

    for payload in (event.order, event.reply):
        candidate = _string_or_none(None if payload is None else payload.get("pair_name"))
        if candidate and candidate in state.pairs:
            return candidate

    client_order_id = _identity_field(event, "clOrdID")
    exchange_order_id = _identity_field(event, "orderID")
    if client_order_id is None and exchange_order_id is None:
        return None

    for pair_name, pair_state in state.pairs.items():
        for identity in (pair_state.head_identity, pair_state.tail_identity):
            if identity is None:
                continue
            if client_order_id and identity.client_order_id == client_order_id:
                return pair_name
            if exchange_order_id and identity.exchange_order_id == exchange_order_id:
                return pair_name
    return None


def _pair_state_with_supervisor_metadata(
    pair_state: PairCycleState,
    *,
    event: EggMove,
    commands: tuple[RuntimeCommand, ...],
) -> PairCycleState:
    command_id = _command_identity(commands[0]) if commands else pair_state.last_emitted_command_id
    return replace(
        pair_state,
        pair_id=pair_state.pair_id or pair_state.pair.name,
        last_processed_private_event_id=(
            event.event_id if event.is_private else pair_state.last_processed_private_event_id
        ),
        last_processed_private_event_ts=(
            event.occurred_at if event.is_private else pair_state.last_processed_private_event_ts
        ),
        last_emitted_command_id=command_id,
        last_emitted_command_ts=(
            event.occurred_at if commands else pair_state.last_emitted_command_ts
        ),
    )


def _identity_field(event: EggMove, field: str) -> str | None:
    for payload in (event.order, event.reply):
        if payload is None:
            continue
        candidate = _string_or_none(payload.get(field))
        if candidate:
            return candidate
    return None


def _command_identity(command: RuntimeCommand) -> str | None:
    if command.order is None:
        return None
    return _string_or_none(command.order.get("clOrdID")) or _string_or_none(command.order.get("orderID"))


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None
