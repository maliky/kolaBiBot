from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kolabi.bot.chronos import PendingRepeat
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
from kolabi.shared.core.runtime_types import (
    AmendTailCommand,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    PrivateOrderRecord,
    RuntimeCommandKind,
    Symbol,
)


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


def test_strategy_runtime_live_mode_does_not_emit_state_followups_from_ack() -> None:
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
    assert followups == ()


def test_tail_submitted_followup_uses_exchange_rounded_ack_price_in_simulation() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=True,
    )
    command = PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="sell",
            ordType="Stop",
            orderQty=Decimal("1"),
            stopPx=Decimal("99.49"),
            clOrdID="CID-T",
        ),
    )

    followups = runtime._followup_events(
        command,
        OrderAck(order_id="OID-T", status="New", price=99.5),
    )

    assert followups[0].reply is not None
    assert followups[0].reply["stopPx"] == 99.5


def test_runtime_resolves_private_record_from_live_command_identity_without_ack_state() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SimulatedExecutor(),
        simulate=False,
    )
    command = PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name="pair-a",
        request=PlaceOrderCommandRequest(
            pair_name="pair-a",
            side="buy",
            ordType="Market",
            orderQty=Decimal("1"),
            clOrdID="CID-HEAD-LIVE",
        ),
    )
    identity = runtime._command_identity_from_command(command)
    assert identity is not None
    runtime._live_command_identities["test"] = identity
    record = PrivateOrderRecord(
        symbol="PI_XBTUSD",
        status="filled",
        client_order_id="CID-HEAD-LIVE",
    )

    resolved = runtime.pair_state_for_record(record)

    assert resolved is not None
    pair_state, role = resolved
    assert pair_state.pair.name == "pair-a"
    assert role == OrderRole.HEAD


def test_strategy_runtime_dispatches_exchange_commands_without_blocking_loop() -> None:
    class SlowExecutor:
        async def execute(self, command):
            await asyncio.sleep(0.2)
            return OrderAck(order_id="OID", status="New", price=100.0)

    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=SlowExecutor(),
        simulate=False,
    )
    event = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime.now(timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
    )

    async def _run() -> bool:
        await runtime.start()
        await runtime.enqueue(event)
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.05)
        command_pending = bool(runtime._inflight_commands)
        await runtime.stop()
        await task
        return command_pending

    assert asyncio.run(_run()) is True


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


def test_strategy_runtime_does_not_exit_while_repeat_is_pending() -> None:
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=sample_strategy()),
        symbol="PI_XBTUSD",
        executor=None,
        simulate=False,
    )
    pair = sample_strategy()[0]
    terminal = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.CLOSED,
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": terminal})
    runtime.chronos.state = runtime.state
    now = datetime.now(timezone.utc)
    runtime.chronos.pending_repeats["pair-a"] = PendingRepeat(
        pair_name="pair-a",
        ready_at=now + timedelta(seconds=5),
        next_attempt=2,
    )

    async def _run() -> bool:
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.15)
        still_running = not task.done()
        await runtime.stop()
        await task
        return still_running

    assert asyncio.run(_run()) is True


def test_public_polling_emits_market_ticks_for_living_tails() -> None:
    class Market:
        best_bid = 102.0
        best_ask = 102.5
        mid_price = 102.25
        last_price = 102.0
        mark_price = None
        index_price = None
        tick_size = 0.5
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
        tick_size = 0.5
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
        tick_size = 0.5
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


def test_update_log_closed_hooked_includes_head_fill_fields(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=True,
    )
    runtime._record_head_lifecycle(
        EggMove(
            kind=EggMoveKind.PLAYED_NOT_CANCELED,
            occurred_at=now,
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            role=OrderRole.HEAD,
            reply={"side": "buy", "cumQty": 1.0, "price": 100.0},
        )
    )
    previous = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=None,
        played_quantity=Decimal("1"),
    )
    current = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        tail_trail=initial_tail_trail(pair, Decimal("100"), now),
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": current})

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_living_updates({"pair-a": previous})

    assert "UPDATE (pair-a#1): (closed--hooked)" in caplog.text
    assert "HFS=buy" in caplog.text
    assert "HFQ=1.00" in caplog.text
    assert "HFP=100.00" in caplog.text


def test_update_log_closed_submitted_omits_head_fill_fields(caplog) -> None:
    pair = sample_strategy()[0]
    now = datetime.now(timezone.utc)
    runtime = StrategyRuntime(
        strategy=StrategySpec(name="demo", pairs=(pair,)),
        symbol="PI_XBTUSD",
        simulate=True,
    )
    trail = replace(
        initial_tail_trail(pair, Decimal("100"), now),
        confirmed_stop_price=Decimal("99.0"),
        last_confirmed_at=now,
    )
    previous = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    current = PairCycleState(
        pair=pair,
        head_state=HeadState.CLOSED,
        tail_state=TailState.SUBMITTED,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=trail,
        played_quantity=Decimal("1"),
    )
    runtime.state = replace(runtime.state, pairs={"pair-a": current})

    with caplog.at_level("INFO", logger="kola"):
        runtime._log_living_updates({"pair-a": previous})

    assert "UPDATE (pair-a#1): (closed--submitted)" in caplog.text
    assert "TCID=CID-T" in caplog.text
    assert "TOID=OID-T" in caplog.text
    assert "HFS=" not in caplog.text


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
