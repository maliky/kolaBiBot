from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from decimal import Decimal

from kolabi.bot.domain import (
    HeadSpec,
    HeadState,
    OrderPairSpec,
    PairCycleState,
    Side,
    StrategyState,
    TailSpec,
    TailState,
    TimeWindow,
)
from kolabi.bot.runtime_policy import (
    CommandSlot,
    active_pair_names,
    append_pending_command,
    command_slot,
    command_slot_still_live,
    head_capacity_available,
)
from kolabi.shared.core.runtime_types import (
    AmendOrderCommandRequest,
    AmendTailCommand,
    CancelCommand,
    CancelOrderCommandRequest,
    PlaceHeadCommand,
    PlaceOrderCommandRequest,
    PlaceTailCommand,
    RuntimeCommandKind,
    Symbol,
)


def _pair(name: str) -> OrderPairSpec:
    return OrderPairSpec(
        name=name,
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
    )


def _state(*pairs: PairCycleState, launched_at: datetime) -> StrategyState:
    return StrategyState(
        launched_at=launched_at,
        pairs={pair_state.pair.name: pair_state for pair_state in pairs},
        strategy_id="policy-test",
    )


def _place_head(pair_name: str) -> PlaceHeadCommand:
    return PlaceHeadCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name=pair_name,
        request=PlaceOrderCommandRequest(
            pair_name=pair_name,
            side="buy",
            ordType="Limit",
            orderQty=Decimal("1"),
            clOrdID=f"CID-H-{pair_name}",
        ),
    )


def _place_tail(pair_name: str) -> PlaceTailCommand:
    return PlaceTailCommand(
        kind=RuntimeCommandKind.PLACE,
        symbol=Symbol("PI_XBTUSD"),
        pair_name=pair_name,
        request=PlaceOrderCommandRequest(
            pair_name=pair_name,
            side="sell",
            ordType="Stop",
            orderQty=Decimal("1"),
            stopPx=Decimal("99"),
            clOrdID=f"CID-T-{pair_name}",
        ),
    )


def _amend_tail(pair_name: str, price: str) -> AmendTailCommand:
    return AmendTailCommand(
        kind=RuntimeCommandKind.AMEND,
        symbol=Symbol("PI_XBTUSD"),
        pair_name=pair_name,
        request=AmendOrderCommandRequest(
            pair_name=pair_name,
            side="sell",
            ordType="Stop",
            orderID=f"OID-T-{pair_name}",
            clOrdID=f"CID-T-{pair_name}",
            newPrice=Decimal(price),
        ),
    )


def _cancel(pair_name: str) -> CancelCommand:
    return CancelCommand(
        kind=RuntimeCommandKind.CANCEL,
        symbol=Symbol("PI_XBTUSD"),
        pair_name=pair_name,
        request=CancelOrderCommandRequest(
            pair_name=pair_name,
            clOrdID=f"CID-H-{pair_name}",
        ),
    )


def test_command_slot_uses_current_attempt_and_command_role() -> None:
    now = datetime.now(timezone.utc)
    state = _state(
        PairCycleState(
            pair=_pair("pair-a"),
            attempt_index=3,
        ),
        launched_at=now,
    )

    assert command_slot(_place_head("pair-a"), state=state) == CommandSlot(
        "pair-a",
        3,
        "head",
    )
    assert command_slot(_place_tail("pair-a"), state=state) == CommandSlot(
        "pair-a",
        3,
        "tail",
    )
    assert command_slot(_cancel("pair-a"), state=state) == CommandSlot(
        "pair-a",
        3,
        "cancel",
    )
    assert command_slot(_place_head("missing"), state=state) == CommandSlot(
        "missing",
        1,
        "head",
    )


def test_active_pair_names_ignore_latent_and_terminal_pairs() -> None:
    now = datetime.now(timezone.utc)
    latent = PairCycleState(pair=_pair("latent"))
    done = PairCycleState(
        pair=_pair("done"),
        head_state=HeadState.CLOSED,
        played_quantity=Decimal("0"),
    )
    working_head = PairCycleState(
        pair=_pair("working-head"),
        head_state=HeadState.SUBMITTED,
    )
    working_tail = PairCycleState(
        pair=_pair("working-tail"),
        head_state=HeadState.CLOSED,
        tail_state=TailState.LIVING,
        played_quantity=Decimal("1"),
    )
    state = _state(
        latent,
        done,
        working_head,
        working_tail,
        launched_at=now,
    )

    assert active_pair_names(state, now=now) == frozenset(
        {"working-head", "working-tail"}
    )


def test_inflight_head_tail_and_tail_amend_commands_count_as_active() -> None:
    now = datetime.now(timezone.utc)
    state = _state(
        PairCycleState(pair=_pair("head")),
        PairCycleState(pair=_pair("tail")),
        PairCycleState(pair=_pair("amend")),
        launched_at=now,
    )

    assert active_pair_names(
        state,
        inflight_commands=(
            (CommandSlot("head", 1, "head"), _place_head("head")),
            (CommandSlot("tail", 1, "tail"), _place_tail("tail")),
            (CommandSlot("amend", 1, "tail"), _amend_tail("amend", "99.5")),
        ),
        now=now,
    ) == frozenset({"head", "tail", "amend"})


def test_head_capacity_allows_same_pair_and_blocks_new_pair_at_capacity() -> None:
    now = datetime.now(timezone.utc)
    state = _state(
        PairCycleState(pair=_pair("pair-a")),
        PairCycleState(pair=_pair("pair-b")),
        launched_at=now,
    )
    inflight = ((CommandSlot("pair-a", 1, "head"), _place_head("pair-a")),)

    assert head_capacity_available(
        _place_head("pair-a"),
        state=state,
        max_active_pairs=1,
        inflight_commands=inflight,
        now=now,
    )
    assert not head_capacity_available(
        _place_head("pair-b"),
        state=state,
        max_active_pairs=1,
        inflight_commands=inflight,
        now=now,
    )
    assert head_capacity_available(
        _place_head("pair-b"),
        state=state,
        max_active_pairs=0,
        inflight_commands=inflight,
        now=now,
    )


def test_command_slot_liveness_rejects_stale_and_closed_roles() -> None:
    now = datetime.now(timezone.utc)
    pair_a = PairCycleState(
        pair=_pair("pair-a"),
        head_state=HeadState.SUBMITTED,
        tail_state=TailState.LIVING,
        attempt_index=2,
    )
    pair_b = PairCycleState(
        pair=_pair("pair-b"),
        head_state=HeadState.FAILED,
    )
    pair_c = PairCycleState(
        pair=_pair("pair-c"),
        head_state=HeadState.CLOSED,
        tail_state=TailState.CLOSED,
        played_quantity=Decimal("1"),
    )
    state = _state(pair_a, pair_b, pair_c, launched_at=now)

    assert command_slot_still_live(CommandSlot("pair-a", 2, "head"), state=state)
    assert command_slot_still_live(CommandSlot("pair-a", 2, "tail"), state=state)
    assert not command_slot_still_live(CommandSlot("pair-a", 1, "head"), state=state)
    assert not command_slot_still_live(CommandSlot("pair-b", 1, "head"), state=state)
    assert not command_slot_still_live(CommandSlot("pair-c", 1, "tail"), state=state)
    assert not command_slot_still_live(CommandSlot("missing", 1, "head"), state=state)


def test_pending_tail_amends_collapse_to_latest_amend() -> None:
    first = _amend_tail("pair-a", "99.5")
    latest = _amend_tail("pair-a", "100.0")
    pending = append_pending_command(deque((first,)), latest)
    request = pending[-1].request

    assert tuple(pending) == (latest,)
    assert request.newPrice == Decimal("100.0")


def test_non_tail_amend_pending_command_is_appended() -> None:
    tail_amend = _amend_tail("pair-a", "99.5")
    head = _place_head("pair-a")
    pending = append_pending_command(deque((tail_amend,)), head)

    assert tuple(pending) == (tail_amend, head)
