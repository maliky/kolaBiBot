from __future__ import annotations

from datetime import datetime, timezone

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


def sample_state() -> StrategyState:
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


def test_step_strategy_returns_fresh_strategy_state_and_updates_one_pair() -> None:
    state = sample_state()
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-1",
    )

    next_state, intents = step_strategy(state, move)

    assert next_state is not state
    assert next_state.pairs["pair-a"] is state.pairs["pair-a"]
    assert next_state.pairs["pair-c"] is state.pairs["pair-c"]
    assert next_state.pairs["pair-b"] is not state.pairs["pair-b"]
    assert next_state.pairs["pair-b"].head_state.value == "hooked"
    assert intents == (PairIntent(PairIntentKind.PLACE_HEAD),)


def test_step_strategy_does_not_create_missing_pair_and_returns_no_intents() -> None:
    state = sample_state()
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-z",
        event_id="evt-2",
    )

    next_state, intents = step_strategy(state, move)

    assert next_state is not state
    assert next_state == state
    assert "pair-z" not in next_state.pairs
    assert intents == ()


def test_step_strategy_unresolved_event_leaves_metadata_unchanged() -> None:
    state = sample_state()
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        event_id="evt-3",
    )

    next_state, intents = step_strategy(state, move)

    assert next_state is not state
    assert next_state.last_event_id is None
    assert next_state.last_event_ts is None
    assert next_state.pairs["pair-a"] == state.pairs["pair-a"]
    assert intents == ()


def test_step_strategy_updates_private_metadata_only_for_private_events() -> None:
    state = sample_state()
    occurred_at = datetime(2026, 5, 21, 12, 4, tzinfo=timezone.utc)
    move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=occurred_at,
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-4",
        is_private=True,
    )

    next_state, _ = step_strategy(state, move)
    pair_state = next_state.pairs["pair-b"]

    assert next_state.last_event_id == "evt-4"
    assert next_state.last_event_ts == occurred_at
    assert pair_state.last_processed_private_event_id == "evt-4"
    assert pair_state.last_processed_private_event_ts == occurred_at
    assert pair_state.pair_id == "pair-b"


def test_step_strategy_public_event_does_not_touch_private_metadata() -> None:
    state = sample_state()
    occurred_at = datetime(2026, 5, 21, 12, 5, tzinfo=timezone.utc)
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=occurred_at,
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        event_id="evt-5",
        is_private=False,
    )

    next_state, intents = step_strategy(state, move)
    pair_state = next_state.pairs["pair-a"]

    assert intents == (PairIntent(PairIntentKind.PLACE_HEAD),)
    assert pair_state.last_processed_private_event_id is None
    assert pair_state.last_processed_private_event_ts is None
    assert pair_state.last_emitted_command_id == PairIntentKind.PLACE_HEAD.value
    assert pair_state.last_emitted_command_ts == occurred_at


def test_step_strategy_preserves_intent_order_from_reducer(monkeypatch) -> None:
    state = sample_state()
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        event_id="evt-6",
    )

    ordered_intents = (
        PairIntent(PairIntentKind.PLACE_HEAD),
        PairIntent(PairIntentKind.PLACE_TAIL),
        PairIntent(PairIntentKind.AMEND_TAIL),
    )

    def fake_step_pair(pair_state: PairCycleState, event: EggMove):
        return pair_state, ordered_intents

    monkeypatch.setattr("kolabi.bot.isis.step_pair", fake_step_pair)

    next_state, intents = step_strategy(state, move)

    assert intents == ordered_intents
    assert next_state.pairs["pair-a"].last_emitted_command_id == PairIntentKind.AMEND_TAIL.value


def test_step_strategy_ignores_contradictory_payload_identity_when_targeted() -> None:
    state = sample_state()
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 7, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        order={"pair_name": "pair-a", "clOrdID": "CID-A"},
        reply={"orderID": "OID-A"},
        event_id="evt-7",
    )

    first_state, first_intents = step_strategy(state, move)
    second_state, second_intents = step_strategy(state, move)

    assert first_state.pairs["pair-b"].head_state.value == "hooked"
    assert first_state.pairs["pair-a"] == state.pairs["pair-a"]
    assert first_intents == second_intents == (PairIntent(PairIntentKind.PLACE_HEAD),)
    assert first_state == second_state
