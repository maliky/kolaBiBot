"""Janus : Pure intent-to-command planner.

Purpose: translate ordered pair intents into typed runtime commands without
touching exchange clients or supervisor state.
Inputs: `PairCycleState`, ordered `PairIntent`, and symbol context.
Outputs: ordered `RuntimeCommand` values.
Side effects: none.
Important types: `PairCycleState`, `PairIntent`, `RuntimeCommand`.
Role: pure logic.
"""
from __future__ import annotations

from kolabi.bot.domain import PairCycleState, PairIntent, PairIntentKind
from kolabi.bot.order_building import head_order_dict, tail_command
from kolabi.shared.core.runtime_types import OrderRole, RuntimeCommand, RuntimeCommandKind, Symbol


def plan_runtime_commands(
    state: PairCycleState,
    intents: tuple[PairIntent, ...],
    *,
    symbol: Symbol,
) -> tuple[RuntimeCommand, ...]:
    """Traduit des intents ordonnes en commandes runtime ordonnees."""
    commands: list[RuntimeCommand] = []
    for intent in intents:
        if intent.kind == PairIntentKind.PLACE_HEAD:
            commands.append(
                RuntimeCommand(
                    kind=RuntimeCommandKind.PLACE,
                    symbol=symbol,
                    order=head_order_dict(state.pair),
                    reason=OrderRole.HEAD.value,
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
