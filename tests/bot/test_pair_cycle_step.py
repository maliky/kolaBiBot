from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from kolabi.bot.domain import (
    EggMove,
    EggMoveKind,
    HeadSpec,
    HeadState,
    OrderRole,
    OrderPairSpec,
    OrderIdentity,
    PairCycleState,
    PairIntentKind,
    Side,
    TailMode,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.pair_cycle import step_pair


def sample_pair() -> OrderPairSpec:
    return OrderPairSpec(
        name="pair-a",
        window=TimeWindow(start_minutes=-1.0, end_minutes=1.0),
        try_num=1,
        dr_pause=None,
        timeout=None,
        head=HeadSpec(
            side=Side.BUY,
            order_type="Limit",
            delta=None,
        ),
        head_price=(100.0, 101.0),
        head_price_type="pA",
        head_quantity=2,
        head_quantity_type="qA",
        tail=TailSpec(
            side=Side.SELL,
            order_type="Stop",
            delta=0.5,
        ),
        tail_price_spec=99.0,
        tail_price_spec_type="tA",
        amount_type="qApD",
        hook_name=None,
    )


def egg_move(kind: EggMoveKind, *, played_quantity: float = 0.0) -> EggMove:
    return EggMove(
        kind=kind,
        occurred_at=datetime.now(timezone.utc),
        symbol="PI_XBTUSD",
        reply={
            "orderID": "OID-1",
            "clOrdID": "CID-1",
            "cumQty": played_quantity,
            "orderQty": 2.0,
            "reference_price": 100.0,
        },
    )


def submitted_state() -> PairCycleState:
    return PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.SUBMITTED,
    )


def test_step_pair_played_open_head_keeps_flapping_tail_living() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_state == TailState.LIVING
    assert next_state.tail_mode == TailMode.FLAPPING
    assert next_state.tail_trail is not None
    assert next_state.played_quantity == Decimal("2.0")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_step_pair_head_failed_keeps_tail_latent() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.NOT_PLAYED_CANCELED),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_state == TailState.LATENT
    assert intents == ()


def test_partial_fill_tail_uses_played_quantity_not_planned_quantity() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.LIVING
    assert next_state.tail_state == TailState.LIVING
    assert next_state.tail_mode == TailMode.FLAPPING
    assert next_state.played_quantity == Decimal("1.0")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_step_pair_zero_fill_cancel_fails_without_tail() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.NOT_PLAYED_CANCELED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.FAILED
    assert next_state.tail_state == TailState.LATENT
    assert next_state.tail_mode is None
    assert intents == ()


def test_step_pair_canceled_head_after_fill_leaves_flying_tail() -> None:
    next_state, intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_AND_CANCELED, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_state == TailState.HOOKED
    assert next_state.tail_mode == TailMode.FLYING
    assert next_state.tail_trail is not None
    assert next_state.played_quantity == Decimal("1.0")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_failed_head_ignores_later_played_event() -> None:
    failed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.FAILED,
        tail_state=TailState.LATENT,
        played_quantity=Decimal("0"),
    )
    next_state, intents = step_pair(
        failed,
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    assert next_state == failed
    assert intents == ()


def test_closed_head_ignores_later_partial_fill_event() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
        played_quantity=Decimal("1.0"),
    )
    next_state, intents = step_pair(
        closed,
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=1.5),
    )

    assert next_state == closed
    assert intents == ()


def test_duplicate_head_filled_does_not_submit_second_tail() -> None:
    first_state, first_intents = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )
    second_state, second_intents = step_pair(
        first_state,
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    assert len(first_intents) == 1
    assert first_intents[0].kind == PairIntentKind.PLACE_TAIL
    assert second_state == first_state
    assert second_intents == ()


def test_closed_played_head_keeps_existing_flying_tail_living() -> None:
    partially_played = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.LIVING,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLAPPING,
        played_quantity=Decimal("1.0"),
    )
    next_state, intents = step_pair(
        partially_played,
        egg_move(EggMoveKind.PLAYED_AND_CANCELED, played_quantity=1.0),
    )

    assert next_state.head_state == HeadState.CLOSED
    assert next_state.tail_state == TailState.LIVING
    assert next_state.tail_mode == TailMode.FLYING
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.PLACE_TAIL


def test_rest_ack_marks_submitted_only() -> None:
    hooked = PairCycleState(pair=sample_pair(), head_state=HeadState.HOOKED)
    next_state, intents = step_pair(
        hooked,
        egg_move(EggMoveKind.HEAD_SUBMITTED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.SUBMITTED
    assert next_state.tail_state is None
    assert intents == ()


def test_private_confirmation_required_for_tail_hook() -> None:
    submitted = submitted_state()
    next_state, intents = step_pair(
        submitted,
        egg_move(EggMoveKind.NOT_PLAYED_NOR_CANCELED, played_quantity=0.0),
    )

    assert next_state.head_state == HeadState.NEW
    assert intents == ()


def test_market_tick_with_tail_identity_emits_amend_only_on_improvement() -> None:
    played, _ = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )
    identified = PairCycleState(
        pair=played.pair,
        head_state=played.head_state,
        tail_state=played.tail_state,
        tail_mode=played.tail_mode,
        tail_identity=OrderIdentity(
            pair_name="pair-a",
            role="tail",
            client_order_id="CID-T",
            exchange_order_id="OID-T",
        ),
        tail_trail=played.tail_trail,
        played_quantity=played.played_quantity,
    )

    moved, intents = step_pair(
        identified,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )
    repeated, repeated_intents = step_pair(
        moved,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )

    assert moved.tail_trail is not None
    assert moved.tail_trail.current_stop_price > Decimal("100")
    assert len(intents) == 1
    assert intents[0].kind == PairIntentKind.AMEND_TAIL
    assert repeated == moved or repeated.tail_trail is not None
    assert repeated_intents == ()


def test_market_tick_before_tail_identity_updates_state_without_amend() -> None:
    played, _ = step_pair(
        submitted_state(),
        egg_move(EggMoveKind.PLAYED_NOT_CANCELED, played_quantity=2.0),
    )

    moved, intents = step_pair(
        played,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )

    assert moved.tail_trail is not None
    assert moved.tail_trail.current_stop_price > Decimal("100")
    assert intents == ()


def test_closed_pair_ignores_later_market_tick() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        tail_mode=TailMode.FLYING,
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.MARKET_TICK,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"reference_price": 102.0},
        ),
    )

    assert next_state == closed
    assert intents == ()


def test_closed_pair_accepts_tail_submission_identity() -> None:
    closed = PairCycleState(
        pair=sample_pair(),
        head_state=HeadState.CLOSED,
        tail_state=TailState.HOOKED,
        tail_mode=TailMode.FLYING,
    )

    next_state, intents = step_pair(
        closed,
        EggMove(
            kind=EggMoveKind.TAIL_SUBMITTED,
            occurred_at=datetime.now(timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-a",
            reply={"orderID": "OID-T", "clOrdID": "CID-T"},
        ),
    )

    assert next_state.tail_state == TailState.SUBMITTED
    assert next_state.tail_identity == OrderIdentity(
        pair_name="pair-a",
        role="tail",
        client_order_id="CID-T",
        exchange_order_id="OID-T",
    )
    assert intents == ()
