from __future__ import annotations

import asyncio

from kolabi.bot.domain import HeadSpec, OrderPairSpec, Side, TailSpec, TimeWindow, TailState
from kolabi.bot.strategy_runtime import SimulatedExecutor, StrategyRuntime


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


def test_strategy_runtime_plans_with_chronos_path() -> None:
    from kolabi.bot.domain import StrategySpec

    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=None,
    )
    result = asyncio.run(runtime.plan())

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
    assert result.state.pairs["pair-a"].tail_state in {TailState.HOOKED, TailState.LIVING}
