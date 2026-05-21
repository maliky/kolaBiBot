from __future__ import annotations

from datetime import datetime, timezone

from pytest_bdd import given, scenario, then, when

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    OrderPairSpec,
    PairCycleState,
    PairIntent,
    PairIntentKind,
    Side,
    StrategyState,
    TailSpec,
    TimeWindow,
)
from kolabi.bot.isis import step_strategy


@scenario("features/isis_functional_core.feature", "Routed event changes only one pair")
def test_routed_event_changes_only_one_pair() -> None:
    pass


@scenario("features/isis_functional_core.feature", "Unresolved event is a deterministic no-op")
def test_unresolved_event_is_a_deterministic_noop() -> None:
    pass


@scenario("features/isis_functional_core.feature", "Private routed event updates private metadata only")
def test_private_routed_event_updates_private_metadata_only() -> None:
    pass


@scenario("features/isis_functional_core.feature", "Isis preserves reducer intent order")
def test_isis_preserves_reducer_intent_order() -> None:
    pass


def sample_pair(name: str) -> OrderPairSpec:
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
    )


@given("a strategy state with three pairs for Isis", target_fixture="state")
def given_strategy_state_with_three_pairs_for_isis() -> StrategyState:
    launched_at = datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc)
    return StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-1",
        pairs={
            "pair-a": PairCycleState(pair=sample_pair("pair-a")),
            "pair-b": PairCycleState(pair=sample_pair("pair-b")),
            "pair-c": PairCycleState(pair=sample_pair("pair-c")),
        },
    )


@when("Isis processes a targeted event for pair B", target_fixture="routed_result")
def when_isis_processes_a_targeted_event_for_pair_b(
    state: StrategyState,
) -> tuple[StrategyState, tuple[PairIntent, ...], StrategyState]:
    before = state
    next_state, intents = step_strategy(
        state,
        EggMove(
            kind=EggMoveKind.HEAD_HOOKED,
            occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-b",
            event_id="evt-1",
        ),
    )
    return next_state, intents, before


@when("Isis processes an event without a target pair", target_fixture="noop_result")
def when_isis_processes_an_event_without_a_target_pair(
    state: StrategyState,
) -> tuple[StrategyState, tuple[PairIntent, ...], StrategyState]:
    before = state
    next_state, intents = step_strategy(
        state,
        EggMove(
            kind=EggMoveKind.HEAD_HOOKED,
            occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            event_id="evt-2",
        ),
    )
    return next_state, intents, before


@when("Isis processes a targeted private event for pair B", target_fixture="private_result")
def when_isis_processes_a_targeted_private_event_for_pair_b(
    state: StrategyState,
) -> tuple[StrategyState, tuple[PairIntent, ...], StrategyState]:
    before = state
    next_state, intents = step_strategy(
        state,
        EggMove(
            kind=EggMoveKind.NOT_PLAYED_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-b",
            event_id="evt-3",
            is_private=True,
        ),
    )
    return next_state, intents, before


@when("Isis receives ordered intents from the pair reducer", target_fixture="ordered_result")
def when_isis_receives_ordered_intents_from_the_pair_reducer(
    state: StrategyState,
    monkeypatch,
) -> tuple[StrategyState, tuple[PairIntent, ...]]:
    ordered_intents = (
        PairIntent(PairIntentKind.PLACE_HEAD),
        PairIntent(PairIntentKind.PLACE_TAIL),
        PairIntent(PairIntentKind.AMEND_TAIL),
    )

    def fake_step_pair(pair_state: PairCycleState, event: EggMove):
        return pair_state, ordered_intents

    monkeypatch.setattr("kolabi.bot.isis.step_pair", fake_step_pair)
    next_state, intents = step_strategy(
        state,
        EggMove(
            kind=EggMoveKind.HEAD_HOOKED,
            occurred_at=datetime(2026, 5, 21, 12, 4, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            event_id="evt-4",
        ),
    )
    return next_state, intents


@then("only pair B state should change in Isis")
def then_only_pair_b_state_should_change_in_isis(
    routed_result: tuple[StrategyState, tuple[PairIntent, ...], StrategyState],
) -> None:
    next_state, _intents, before = routed_result
    assert next_state.pairs["pair-a"] == before.pairs["pair-a"]
    assert next_state.pairs["pair-c"] == before.pairs["pair-c"]
    assert next_state.pairs["pair-b"].head_state.value == "hooked"


@then("Isis should emit one ordered head intent")
def then_isis_should_emit_one_ordered_head_intent(
    routed_result: tuple[StrategyState, tuple[PairIntent, ...], StrategyState],
) -> None:
    _next_state, intents, _before = routed_result
    assert intents == (PairIntent(PairIntentKind.PLACE_HEAD),)


@then("Isis should return a fresh unchanged strategy state")
def then_isis_should_return_a_fresh_unchanged_strategy_state(
    noop_result: tuple[StrategyState, tuple[PairIntent, ...], StrategyState],
) -> None:
    next_state, _intents, before = noop_result
    assert next_state is not before
    assert next_state == before


@then("Isis should emit no intents")
def then_isis_should_emit_no_intents(
    noop_result: tuple[StrategyState, tuple[PairIntent, ...], StrategyState],
) -> None:
    _next_state, intents, _before = noop_result
    assert intents == ()


@then("Isis should update private metadata for pair B")
def then_isis_should_update_private_metadata_for_pair_b(
    private_result: tuple[StrategyState, tuple[PairIntent, ...], StrategyState],
) -> None:
    next_state, _intents, _before = private_result
    assert next_state.pairs["pair-b"].last_processed_private_event_id == "evt-3"


@then("Isis should not update private metadata for other pairs")
def then_isis_should_not_update_private_metadata_for_other_pairs(
    private_result: tuple[StrategyState, tuple[PairIntent, ...], StrategyState],
) -> None:
    next_state, _intents, _before = private_result
    assert next_state.pairs["pair-a"].last_processed_private_event_id is None
    assert next_state.pairs["pair-c"].last_processed_private_event_id is None


@then("Isis should preserve the reducer intent order")
def then_isis_should_preserve_the_reducer_intent_order(
    ordered_result: tuple[StrategyState, tuple[PairIntent, ...]],
) -> None:
    _next_state, intents = ordered_result
    assert intents == (
        PairIntent(PairIntentKind.PLACE_HEAD),
        PairIntent(PairIntentKind.PLACE_TAIL),
        PairIntent(PairIntentKind.AMEND_TAIL),
    )
