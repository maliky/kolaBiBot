"""ISIS : Pure strategy supervisor reducer built above the pair reducer.

Purpose: route one already-targeted event to one pair, delegate lifecycle
semantics to `step_pair`, and emit ordered pair intents without side effects.
Inputs: immutable `StrategyState` and one already-targeted `EggMove`.
Outputs: updated `StrategyState` and ordered `PairIntent` values.
Side effects: none.
Important types: `StrategyState`, `PairCycleState`, `EggMove`, `PairIntent`.
Role: pure logic.
"""
from __future__ import annotations

from dataclasses import replace

from kolabi.bot.domain import EggMove, PairCycleState, PairIntent, StrategyState
from kolabi.bot.pair_cycle import step_pair


def step_strategy(
    state: StrategyState,
    event: EggMove,
) -> tuple[StrategyState, tuple[PairIntent, ...]]:
    """Route un evenement deja cible vers une paire et retourne les intents emis."""
    pair_name = event.pair_name
    if pair_name is None:
        return replace(state), ()

    pair_state = state.pairs.get(pair_name)
    if pair_state is None:
        return replace(state), ()

    next_pair_state, intents = step_pair(pair_state, event)
    next_pair_state = _pair_state_with_supervisor_metadata(
        next_pair_state,
        event=event,
        intents=intents,
    )
    next_state = replace(
        state,
        pairs={**state.pairs, pair_name: next_pair_state},
        last_event_id=event.event_id,
        last_event_ts=event.occurred_at,
    )
    return next_state, intents


def _pair_state_with_supervisor_metadata(
    pair_state: PairCycleState,
    *,
    event: EggMove,
    intents: tuple[PairIntent, ...],
) -> PairCycleState:
    last_intent_id = intents[-1].kind.value if intents else pair_state.last_emitted_command_id
    return replace(
        pair_state,
        pair_id=pair_state.pair_id or pair_state.pair.name,
        last_processed_private_event_id=(
            event.event_id if event.is_private else pair_state.last_processed_private_event_id
        ),
        last_processed_private_event_ts=(
            event.occurred_at if event.is_private else pair_state.last_processed_private_event_ts
        ),
        last_emitted_command_id=last_intent_id,
        last_emitted_command_ts=(
            event.occurred_at if intents else pair_state.last_emitted_command_ts
        ),
    )
