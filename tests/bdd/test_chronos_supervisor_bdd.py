from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from pytest_bdd import given, scenario, then, when

from kolabi.bot.chronos import Chronos, ChronosNotice, ChronosNoticeKind
from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    HeadState,
    OrderIdentity,
    OrderPairSpec,
    PairCycleState,
    Side,
    StrategyState,
    TailMode,
    TailState,
    TailSpec,
    TimeWindow,
)
from kolabi.shared.core.runtime_types import RuntimeCommand


@scenario("features/chronos_supervisor.feature", "Event routing changes only pair B")
def test_event_routing_changes_only_pair_b() -> None:
    pass


@scenario("features/chronos_supervisor.feature", "Duplicate private fill event is ignored")
def test_duplicate_private_fill_event_is_ignored() -> None:
    pass


@scenario("features/chronos_supervisor.feature", "Private terminal event wins over public trigger")
def test_private_terminal_event_wins_over_public_trigger() -> None:
    pass


@scenario("features/chronos_supervisor.feature", "REST ack without private confirmation times out")
def test_rest_ack_without_private_confirmation_times_out() -> None:
    pass


@scenario("features/chronos_supervisor.feature", "Tail chaining activates a dependent pair")
def test_tail_chaining_activates_a_dependent_pair() -> None:
    pass


def sample_pair(name: str, *, hook_name: str | None = None) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
        window=TimeWindow(start_minutes=-1.0, end_minutes=10.0),
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
        hook_name=hook_name,
    )


@given("a strategy state with three pairs for Chronos", target_fixture="chronos")
def given_strategy_state_with_three_pairs() -> Chronos:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    return Chronos(
        state=StrategyState(
            launched_at=launched_at,
            strategy_id="strategy-1",
            pairs={
                "pair-a": PairCycleState(pair=sample_pair("pair-a")),
                "pair-b": PairCycleState(
                    pair=sample_pair("pair-b"),
                    head_state=HeadState.SUBMITTED,
                    head_identity=OrderIdentity(
                        pair_name="pair-b",
                        role="head",
                        client_order_id="CID-B",
                        exchange_order_id="OID-B",
                    ),
                ),
                "pair-c": PairCycleState(pair=sample_pair("pair-c")),
            },
        )
    )


@given("a Chronos instance with a submitted pair B", target_fixture="chronos")
def given_chronos_instance_with_submitted_pair_b() -> Chronos:
    return given_strategy_state_with_three_pairs()


@given("a public trigger and a private terminal event for the same pair", target_fixture="payload")
def given_public_and_private_same_pair() -> tuple[Chronos, list[EggMove]]:
    chronos = given_strategy_state_with_three_pairs()
    return chronos, [
        EggMove(
            kind=EggMoveKind.HEAD_HOOKED,
            occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-b",
            event_id="evt-public",
        ),
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 1, 1, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-b",
            event_id="evt-private",
            is_private=True,
        ),
    ]


@given("a private event without enough identity for confirmation", target_fixture="payload")
def given_private_event_without_identity() -> tuple[Chronos, EggMove]:
    chronos = given_strategy_state_with_three_pairs()
    return chronos, EggMove(
        kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        is_private=True,
    )


@given("a closed tail and a dependent latent pair", target_fixture="payload")
def given_closed_tail_and_dependent_pair() -> tuple[Chronos, EggMove]:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    chronos = Chronos(
        state=StrategyState(
            launched_at=launched_at,
            strategy_id="strategy-chain",
            pairs={
                "pair-x": PairCycleState(
                    pair=sample_pair("pair-x"),
                    head_state=HeadState.CLOSED,
                    tail_state=TailState.CLOSED,
                    tail_mode=TailMode.FLYING,
                    played_quantity=Decimal("1"),
                ),
                "pair-y": PairCycleState(pair=sample_pair("pair-y", hook_name="pair-x-tail-closed")),
            },
        )
    )
    move = EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-x",
        event_id="evt-chain",
        is_private=True,
    )
    return chronos, move


@when("a private event for pair B is processed by Chronos", target_fixture="result")
def when_private_event_for_pair_b(chronos: Chronos) -> tuple[Chronos, tuple[RuntimeCommand, ...], StrategyState]:
    before = chronos.state
    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-b",
            event_id="evt-1",
            is_private=True,
        )
    )
    return chronos, commands, before


@when("the same private fill event is processed twice", target_fixture="result")
def when_same_private_fill_twice(chronos: Chronos) -> tuple[Chronos, tuple[RuntimeCommand, ...], tuple[RuntimeCommand, ...]]:
    move = EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-fill",
        is_private=True,
        reply={"clOrdID": "CID-B", "orderID": "OID-B", "cumQty": 1.0, "orderQty": 1.0},
    )
    first = chronos.process_event(move)
    second = chronos.process_event(move)
    return chronos, first, second


@when("Chronos processes the same-cycle event batch", target_fixture="result")
def when_process_same_cycle_batch(payload: tuple[Chronos, list[EggMove]]) -> tuple[Chronos, tuple[RuntimeCommand, ...]]:
    chronos, events = payload
    return chronos, chronos.process_events(events)


@when("the pending identity timeout expires", target_fixture="result")
def when_pending_identity_timeout_expires(payload: tuple[Chronos, EggMove]) -> tuple[Chronos, tuple[ChronosNotice, ...]]:
    chronos, move = payload
    chronos.process_event(move, now=move.occurred_at)
    notices = chronos.expire_pending(now=move.occurred_at + timedelta(seconds=31))
    return chronos, notices


@when("Chronos processes the upstream private closing event", target_fixture="result")
def when_chronos_processes_upstream_private_closing_event(payload: tuple[Chronos, EggMove]) -> tuple[Chronos, tuple[RuntimeCommand, ...]]:
    chronos, move = payload
    return chronos, chronos.process_event(move)


@then("only pair B state should change")
def then_only_pair_b_changes(result: tuple[Chronos, tuple[RuntimeCommand, ...], StrategyState]) -> None:
    chronos, _commands, before = result
    assert chronos.state.pairs["pair-a"] == before.pairs["pair-a"]
    assert chronos.state.pairs["pair-c"] == before.pairs["pair-c"]
    assert chronos.state.pairs["pair-b"].head_state == HeadState.FAILED


@then("pairs A and C should emit no commands")
def then_pairs_a_and_c_emit_no_commands(result: tuple[Chronos, tuple[RuntimeCommand, ...], StrategyState]) -> None:
    _chronos, commands, _before = result
    assert commands == ()


@then("Chronos should emit commands only once for that logical event")
def then_emit_commands_only_once(result: tuple[Chronos, tuple[RuntimeCommand, ...], tuple[RuntimeCommand, ...]]) -> None:
    _chronos, first, second = result
    assert first
    assert second == ()


@then("Chronos should record DuplicateEventIgnored")
def then_record_duplicate_event_ignored(result: tuple[Chronos, tuple[RuntimeCommand, ...], tuple[RuntimeCommand, ...]]) -> None:
    chronos, _first, _second = result
    assert chronos.notices[-1].kind == ChronosNoticeKind.DUPLICATE_EVENT_IGNORED


@then("the private terminal event should win")
def then_private_terminal_wins(result: tuple[Chronos, tuple[RuntimeCommand, ...]]) -> None:
    chronos, commands = result
    assert commands == ()
    assert chronos.state.pairs["pair-b"].head_state == HeadState.FAILED


@then("the public trigger should be recorded as ignored")
def then_public_trigger_recorded_as_ignored(result: tuple[Chronos, tuple[RuntimeCommand, ...]]) -> None:
    chronos, _commands = result
    assert any(notice.kind == ChronosNoticeKind.PUBLIC_EVENT_IGNORED for notice in chronos.notices)


@then("Chronos should emit a typed pending identity timeout notice")
def then_pending_identity_timeout_notice(result: tuple[Chronos, tuple[ChronosNotice, ...]]) -> None:
    _chronos, notices = result
    assert len(notices) == 1
    assert notices[0].kind == ChronosNoticeKind.PENDING_IDENTITY_TIMEOUT


@then("no exchange command should be emitted")
def then_no_exchange_command(result: tuple[Chronos, tuple[ChronosNotice, ...]]) -> None:
    chronos, _notices = result
    assert chronos.command_queue.empty()


@then("the dependent pair should become hooked")
def then_dependent_pair_hooked(result: tuple[Chronos, tuple[RuntimeCommand, ...]]) -> None:
    chronos, _commands = result
    assert chronos.state.pairs["pair-y"].head_state == HeadState.HOOKED


@then("Chronos should forward the next typed runtime command")
def then_forward_next_typed_runtime_command(result: tuple[Chronos, tuple[RuntimeCommand, ...]]) -> None:
    _chronos, commands = result
    assert commands
    assert all(isinstance(command, RuntimeCommand) for command in commands)
