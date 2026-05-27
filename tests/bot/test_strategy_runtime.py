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
    OrderRole,
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
    StaticHookSource,
    StrategyRuntime,
    plan_strategy_once,
)
from kolabi.bot.tail_tracking import initial_tail_trail
from kolabi.shared.core.models import OrderAck
from kolabi.shared.core.runtime_types import AmendTailCommand, RuntimeCommandKind, Symbol


async def _run_runtime_for(runtime: StrategyRuntime, *, seconds: float = 0.05):
    task = asyncio.create_task(runtime.run())
    await asyncio.sleep(seconds)
    await runtime.stop()
    return await task


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
    result = asyncio.run(_run_runtime_for(runtime))

    assert result.commands
    assert result.state.pairs["pair-a"].tail_state in {
        TailState.HOOKED,
        TailState.LIVING,
        TailState.SUBMITTED,
    }


def test_strategy_runtime_simulation_initialises_relative_tail_reference() -> None:
    pair = sample_strategy()[0]
    pair = replace(pair, tail_price_spec=1.5, tail_price_spec_type="t%", amount_type="qAt%p%")
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )

    result = asyncio.run(_run_runtime_for(runtime))

    assert result.state.pairs["pair-a"].tail_trail is not None
    assert result.state.pairs["pair-a"].tail_trail.entry_reference_price == Decimal("100.0")


def test_strategy_runtime_live_mode_does_not_synthesise_head_played_from_ack() -> None:
    pair = sample_strategy()[0]
    pair = replace(pair, tail_price_spec=1.5, tail_price_spec_type="t%", amount_type="qAt%p%")

    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=False,
    )
    command = plan_strategy_once(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
    ).commands[0]
    prepared = runtime._prepare_command(command)
    ack = OrderAck(
        order_id="OID-H",
        status="Filled",
        price=100.0,
        orig_qty=1.0,
        executed_qty=1.0,
        side="buy",
    )

    followups = runtime._followup_events(prepared, ack)
    assert len(followups) == 1
    assert followups[0].kind == EggMoveKind.HEAD_SUBMITTED


def test_strategy_runtime_waits_for_tail_after_filled_head() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )
    pair = sample_strategy()[0]

    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                played_quantity=Decimal("1"),
                tail_state=TailState.HOOKED,
            )
        },
    )
    assert runtime.all_pairs_terminal is False

    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                played_quantity=Decimal("1"),
                tail_state=TailState.SUBMITTED,
            )
        },
    )
    assert runtime.all_pairs_terminal is False

    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                played_quantity=Decimal("1"),
                tail_state=TailState.CLOSED,
            )
        },
    )
    assert runtime.all_pairs_terminal is True


def test_public_polling_emits_market_ticks_for_living_tails() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        last_price = 102.0
        mark_price = None
        index_price = None
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

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    runtime = Runtime()
    source = KrakenPublicTriggerSource(Client(), poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert len(runtime.events) == 1
    assert runtime.events[0].kind == EggMoveKind.MARKET_TICK
    assert runtime.events[0].reply is not None
    assert runtime.events[0].reply["reference_price"] == 102.0


def test_public_polling_does_not_deduplicate_changed_tail_reference() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        mark_price = None
        index_price = None
        recorded_at = "same-book-row"

        def __init__(self, last_price: float) -> None:
            self.last_price = last_price

    class Client:
        def __init__(self) -> None:
            self.prices = [102.0, 103.0]

        def fetch_market_state(self, symbol=None):
            return Market(self.prices.pop(0) if self.prices else 103.0)

    class Runtime:
        symbol = "PI_XBTUSD"
        running = True

        def __init__(self) -> None:
            pair = sample_strategy()[0]
            self.state = replace(
                plan_strategy_once(
                    strategy=StrategySpec(name="demo", pairs=(pair,)),
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
            return len(self.events) >= 2

        async def enqueue(self, event: EggMove) -> None:
            self.events.append(event)

        def pair_state_for_record(
            self, record: object
        ) -> tuple[PairCycleState, OrderRole] | None:
            return None

    runtime = Runtime()
    source = KrakenPublicTriggerSource(Client(), poll_seconds=0.0)

    asyncio.run(source.pump(runtime))

    assert [event.reply["reference_price"] for event in runtime.events if event.reply] == [
        102.0,
        103.0,
    ]


def test_market_tick_reaches_horus_as_tail_amend_command() -> None:
    pair = sample_strategy()[0]
    confirmed_at = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), confirmed_at),
        confirmed_stop_price=Decimal("99.0"),
        last_confirmed_at=confirmed_at,
    )
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


def test_tail_telemetry_rows_include_distance_and_last_update() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        last_price = 102.0
        mark_price = None
        index_price = None
        recorded_at = "tick-1"

    class Reader:
        def fetch_market_state(self, symbol=None):
            return Market()

    pair = sample_strategy()[0]
    confirmed_at = datetime.now(timezone.utc)
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), confirmed_at),
        confirmed_stop_price=Decimal("99.0"),
        last_confirmed_at=confirmed_at,
    )
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
        public_state_reader=Reader(),
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.LIVING,
                tail_mode=None,
                tail_trail=trail,
                played_quantity=Decimal("1"),
            )
        },
    )

    rows = runtime._collect_tail_telemetry_rows(datetime.now(timezone.utc))

    assert len(rows) == 1
    row = rows[0]
    assert row.initial_distance == float(trail.baseline_width)
    assert row.current_distance == float(Decimal("102.0") - Decimal("99.0"))
    assert row.last_tail_update_at == confirmed_at


def test_runtime_matches_private_record_to_tail_identity() -> None:
    pair = sample_strategy()[0]
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=False,
    )
    runtime.state = replace(
        runtime.state,
        pairs={
            "pair-a": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.SUBMITTED,
                tail_identity=OrderIdentity(
                    pair_name="pair-a",
                    role="tail",
                    client_order_id="CID-T",
                    exchange_order_id="OID-T",
                ),
                played_quantity=Decimal("1"),
            )
        },
    )

    class _Record:
        client_order_id = "CID-T"
        exchange_order_id = "OID-T"

    matched = runtime.pair_state_for_record(_Record())

    assert matched is not None
    assert matched[1] == OrderRole.TAIL
