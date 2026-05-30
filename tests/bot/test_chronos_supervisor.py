from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from kolabi.bot.chronos import Chronos, ChronosNoticeKind
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
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.shared.core.runtime_types import (
    DragonSong,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    RuntimeCommandKind,
    Symbol,
)


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
    submitted_b = PairCycleState(
        pair=sample_pair("pair-b"),
        head_state=HeadState.SUBMITTED,
        head_identity=OrderIdentity(
            pair_name="pair-b",
            role="head",
            client_order_id="CID-B",
            exchange_order_id="OID-B",
        ),
    )
    return StrategyState(
        launched_at=launched_at,
        strategy_id="strategy-1",
        pairs={
            "pair-a": PairCycleState(pair=sample_pair("pair-a")),
            "pair-b": submitted_b,
            "pair-c": PairCycleState(pair=sample_pair("pair-c")),
        },
    )


def test_chronos_dedupes_duplicate_event() -> None:
    chronos = Chronos(state=sample_state())
    move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-1",
        is_private=True,
    )

    first = chronos.process_event(move)
    second = chronos.process_event(move)

    assert first == ()
    assert second == ()
    assert chronos.state.pairs["pair-b"].head_state == HeadState.FAILED
    assert chronos.notices[-1].kind == ChronosNoticeKind.DUPLICATE_EVENT_IGNORED


def test_chronos_private_terminal_precedence() -> None:
    chronos = Chronos(state=sample_state())
    public_move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-public",
    )
    private_terminal = EggMove(
        kind=EggMoveKind.NOT_PLAYED_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 1, 1, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-b",
        event_id="evt-private",
        is_private=True,
    )

    commands = chronos.process_events([public_move, private_terminal])

    assert commands == ()
    assert chronos.state.pairs["pair-b"].head_state == HeadState.FAILED
    assert any(notice.kind == ChronosNoticeKind.PUBLIC_EVENT_IGNORED for notice in chronos.notices)


def test_chronos_dedupes_duplicate_command() -> None:
    chronos = Chronos(state=sample_state())
    commands: list[DragonSong] = [
        PlaceHeadCommand(
            kind=kind,
            symbol=Symbol("PI_XBTUSD"),
            request=PlaceOrderCommandRequest(
                pair_name="pair-b",
                side="buy",
                ordType="Limit",
                clOrdID="CID-B",
            ),
            pair_name="pair-b",
        )
        for kind in (RuntimeCommandKind.PLACE, RuntimeCommandKind.PLACE)
    ]

    deduped = chronos._dedupe_commands(commands)

    assert len(deduped) == 1


def test_chronos_requires_identity_for_confirmation_match() -> None:
    chronos = Chronos(state=sample_state())
    private_move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        is_private=True,
    )

    commands = chronos.process_event(private_move)

    assert commands == ()
    assert len(chronos.pending) == 1
    assert chronos.state == sample_state()


def test_chronos_pending_identity_timeout_is_typed() -> None:
    chronos = Chronos(state=sample_state(), pending_timeout=timedelta(seconds=5))
    private_move = EggMove(
        kind=EggMoveKind.NOT_PLAYED_NOR_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        is_private=True,
    )
    chronos.process_event(private_move, now=datetime(2026, 5, 21, 12, 2, tzinfo=timezone.utc))

    notices = chronos.expire_pending(now=datetime(2026, 5, 21, 12, 2, 6, tzinfo=timezone.utc))

    assert len(notices) == 1
    assert notices[0].kind == ChronosNoticeKind.PENDING_IDENTITY_TIMEOUT


def test_chronos_emits_no_exchange_payloads_directly() -> None:
    chronos = Chronos(state=sample_state())
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 3, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        event_id="evt-3",
    )

    commands = chronos.process_event(move)

    assert commands
    assert all(isinstance(command, DragonSong.__args__) for command in commands)
    assert all(not isinstance(command, dict) for command in commands)


def test_chronos_emits_typed_runtime_commands_only() -> None:
    chronos = Chronos(state=sample_state())
    move = EggMove(
        kind=EggMoveKind.HEAD_HOOKED,
        occurred_at=datetime(2026, 5, 21, 12, 4, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-a",
        event_id="evt-4",
    )

    commands = chronos.process_event(move)

    assert commands
    assert all(isinstance(command, DragonSong.__args__) for command in commands)


def test_chronos_processes_batch_events() -> None:
    chronos = Chronos(state=sample_state())
    commands = chronos.process_events(
        [
            EggMove(
                kind=EggMoveKind.HEAD_HOOKED,
                occurred_at=datetime(2026, 5, 21, 12, 5, tzinfo=timezone.utc),
                symbol="PI_XBTUSD",
                pair_name="pair-a",
                event_id="evt-5",
            )
        ]
    )

    assert commands
    assert isinstance(commands[0], DragonSong.__args__)


def test_closed_tail_can_hook_dependent_pair() -> None:
    pair_x = sample_pair("pair-x")
    pair_y = replace(sample_pair("pair-y"), hook_name="pair-x-tail-closed")
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-chain",
        pairs={
            "pair-x": PairCycleState(
                pair=pair_x,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
            "pair-y": PairCycleState(pair=pair_y),
        },
    )
    chronos = Chronos(state=state)
    move = EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-x",
        event_id="evt-chain",
        is_private=True,
    )

    commands = chronos.process_event(move)

    assert commands
    assert chronos.state.pairs["pair-y"].head_state == HeadState.HOOKED


def test_tail_closed_hook_does_not_activate_on_head_close_only() -> None:
    pair_x = sample_pair("pair-x")
    pair_y = replace(sample_pair("pair-y"), hook_name="pair-x-tail-closed")
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-chain",
        pairs={
            "pair-x": PairCycleState(
                pair=pair_x,
                head_state=HeadState.SUBMITTED,
                head_identity=OrderIdentity(
                    pair_name="pair-x",
                    role="head",
                    client_order_id="CID-X-H",
                    exchange_order_id="OID-X-H",
                ),
            ),
            "pair-y": PairCycleState(pair=pair_y),
        },
    )
    chronos = Chronos(state=state)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
            symbol="PI_XBTUSD",
            pair_name="pair-x",
            role=None,
            event_id="evt-chain-head-only",
            reply={
                "orderID": "OID-X-H",
                "clOrdID": "CID-X-H",
                "cumQty": 1.0,
                "orderQty": 1.0,
            },
            is_private=True,
        )
    )

    assert commands
    assert {command.pair_name for command in commands} == {"pair-x"}
    assert chronos.state.pairs["pair-x"].head_state == HeadState.CLOSED
    assert chronos.state.pairs["pair-x"].tail_state == TailState.HOOKED
    assert chronos.state.pairs["pair-y"].head_state == HeadState.LATENT


def test_chronos_repeats_terminal_pair_with_fresh_attempt_key() -> None:
    pair = replace(sample_pair("pair-r"), try_num=2)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
                attempt_index=1,
            ),
        },
    )
    chronos = Chronos(state=state)
    move = EggMove(
        kind=EggMoveKind.PLAYED_AND_CANCELED,
        occurred_at=datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc),
        symbol="PI_XBTUSD",
        pair_name="pair-r",
        event_id="evt-repeat-1",
        is_private=True,
    )

    commands = chronos.process_event(move)

    assert commands
    assert chronos.state.pairs["pair-r"].attempt_index == 2
    assert chronos.state.pairs["pair-r"].head_state == HeadState.HOOKED
    assert commands[0].pair_name == "pair-r"


def test_chronos_delays_repeat_until_pause_has_elapsed() -> None:
    pair = replace(sample_pair("pair-r"), try_num=2, dr_pause=1.0)
    state = StrategyState(
        launched_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
        strategy_id="strategy-repeat",
        pairs={
            "pair-r": PairCycleState(
                pair=pair,
                head_state=HeadState.CLOSED,
                tail_state=TailState.CLOSED,
                tail_mode=TailMode.FLYING,
                played_quantity=Decimal("1"),
            ),
        },
    )
    chronos = Chronos(state=state)
    occurred_at = datetime(2026, 5, 21, 12, 6, tzinfo=timezone.utc)

    commands = chronos.process_event(
        EggMove(
            kind=EggMoveKind.PLAYED_AND_CANCELED,
            occurred_at=occurred_at,
            symbol="PI_XBTUSD",
            pair_name="pair-r",
            event_id="evt-repeat-delay",
            is_private=True,
        ),
        now=occurred_at,
    )
    early = chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=occurred_at + timedelta(seconds=30),
    )
    ready = chronos.activate_ready_repeats(
        symbol="PI_XBTUSD",
        now=occurred_at + timedelta(seconds=61),
    )

    assert commands == ()
    assert early == ()
    assert ready
    assert chronos.state.pairs["pair-r"].attempt_index == 2
