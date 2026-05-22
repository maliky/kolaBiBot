"""Janus : Pure intent-to-command planner.

Purpose: translate ordered pair intents into typed bot commands without
touching exchange clients or supervisor state.
Inputs: `PairCycleState`, ordered `PairIntent`, and symbol context.
Outputs: ordered bot command values.
Side effects: none.
Important types: `PairCycleState`, `PairIntent`, `RuntimeCommand`.
Role: pure logic.
"""
from __future__ import annotations

from kolabi.bot.domain import PairCycleState, PairIntent, PairIntentKind
from kolabi.bot.order_building import head_order_dict, head_place_request, tail_command
from kolabi.shared.core.runtime_types import (
    BotCommand,
    PlaceHeadCommand,
    RuntimeCommandKind,
    Symbol,
)


def plan_runtime_commands(
    state: PairCycleState,
    intents: tuple[PairIntent, ...],
    *,
    symbol: Symbol,
) -> tuple[BotCommand, ...]:
    """Traduit des intents ordonnes en commandes runtime ordonnees."""
    commands: list[BotCommand] = []
    for intent in intents:
        if intent.kind == PairIntentKind.PLACE_HEAD:
            commands.append(
                PlaceHeadCommand(
                    kind=RuntimeCommandKind.PLACE,
                    symbol=symbol,
                    request=head_place_request(state.pair),
                    pair_name=state.pair.name,
                    legacy_order=head_order_dict(state.pair),
                )
            )
        elif intent.kind == PairIntentKind.PLACE_TAIL:
            commands.append(
                tail_command(
                    state,
                    symbol=symbol,
                    kind=RuntimeCommandKind.PLACE,
                )
            )
        elif intent.kind == PairIntentKind.AMEND_TAIL:
            commands.append(
                tail_command(
                    state,
                    symbol=symbol,
                    kind=RuntimeCommandKind.AMEND,
                )
            )
        else:
            raise ValueError(f"unsupported pair intent kind: {intent.kind!r}")
    return tuple(commands)
