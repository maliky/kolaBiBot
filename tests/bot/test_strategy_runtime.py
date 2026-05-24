from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    PairCycleState,
    Side,
    StrategySpec,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.horus import plan_runtime_commands
from kolabi.bot.pair_cycle import step_pair
from kolabi.bot.strategy_runtime import (
    KrakenPublicTriggerSource,
    SimulatedExecutor,
    StrategyRuntime,
    plan_strategy_once,
)
from kolabi.bot.tail_tracking import initial_tail_trail
from kolabi.shared.core.runtime_types import AmendTailCommand, RuntimeCommandKind, Symbol


def sample_strategy() -> tuple[OrderPairSpec, ...]:
    return (
        OrderPairSpec(
            name="pair-a",
            window=TimeWindow(start_minutes=0.0, end_minutes=60.0),
            try_num=1,
            dr_pause=None,
            timeout=60,
            head=HeadSpec(side=Side.BUY, order_type="Limit"),
            head_price=(100.0, 101.0),
            head_price_type="pA",
            head_quantity=1,
            head_quantity_type="qA",
            tail=TailSpec(side=Side.SELL, order_type="Stop", delta=0.5),
            tail_price_spec=99.0,
            tail_price_spec_type="tA",
            amount_type="qApD",
        ),
    )


def test_plan_strategy_once_uses_the_chronos_path() -> None:
    from kolabi.bot.domain import StrategySpec

    result = plan_strategy_once(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
    )

    assert result.commands
    assert result.commands[0].pair_name == "pair-a"
    assert result.commands[0].role is not None and result.commands[0].role.value == "head"


def test_strategy_runtime_simulation_advances_to_tail_state() -> None:
    from kolabi.bot.domain import StrategySpec

    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )
    result = asyncio.run(runtime.run())

    assert result.commands
    assert result.state.pairs["pair-a"].tail_state in {
        TailState.HOOKED,
        TailState.LIVING,
        TailState.SUBMITTED,
    }


def test_public_polling_emits_market_ticks_for_living_tails() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        recorded_at = "tick-1"

    class Client:
        def fetch_market_state(self, symbol=None):
            return Market()

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            pair = sample_strategy()[0]
            self.state = replace(
                plan_strategy_once(
                    strategy=StrategySpec(
                        name="demo",
                        pairs=(pair,),
                    ),
                    symbol="PI_XBTUSD",
                ).state,
                pairs={
                    "pair-a": PairCycleState(
                        pair=pair,
                        head_state=HeadState.LIVING,
                        tail_state=TailState.LIVING,
                        tail_trail=initial_tail_trail(
                            pair,
                            Decimal("100"),
                            datetime.now(timezone.utc),
                        ),
                    )
                },
            )
            self.events: list[EggMove] = []

        @property
        def all_pairs_terminal(self) -> bool:
            return bool(self.events)

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def record_targets_head(self, record: object) -> bool:
            return False

    runtime = Runtime()
    source = KrakenPublicTriggerSource(Client(), poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert len(runtime.events) == 1
    assert runtime.events[0].kind == EggMoveKind.MARKET_TICK
    assert runtime.events[0].reply is not None
    assert runtime.events[0].reply["reference_price"] == 102.0


def test_market_tick_reaches_horus_as_tail_amend_command() -> None:
    pair = sample_strategy()[0]
    trail = initial_tail_trail(pair, Decimal("100"), datetime.now(timezone.utc))
    state = PairCycleState(
        pair=pair,
        head_state=HeadState.LIVING,
        tail_state=TailState.LIVING,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )

    next_state, intents = step_pair(
        state,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )
    commands = plan_runtime_commands(next_state, intents, symbol=Symbol("PI_XBTUSD"))

    assert len(commands) == 1
    assert isinstance(commands[0], AmendTailCommand)
    assert commands[0].kind == RuntimeCommandKind.AMEND
    assert commands[0].request.newPrice is not None
    assert commands[0].request.newPrice > Decimal("100")
