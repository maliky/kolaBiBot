from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from kolabi.bot.domain import EggMoveKind
from kolabi.bot.dragon import (
    PrivateOrderFact,
    head_move_from_private_fact,
    head_submitted_from_ack,
    simulated_private_fill_from_submission,
)
from kolabi.shared.core.models import OrderAck


def test_head_submitted_from_ack_is_submission_only() -> None:
    move = head_submitted_from_ack(
        pair_name="pair-a",
        symbol="PI_XBTUSD",
        ack=OrderAck(order_id="OID-1", status="New", orig_qty=1.0, side="buy"),
        client_order_id="CID-1",
        occurred_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert move.kind == EggMoveKind.HEAD_SUBMITTED
    assert move.pair_name == "pair-a"
    assert move.is_private is False


def test_private_partial_fill_maps_to_played_not_canceled() -> None:
    fact = PrivateOrderFact(
        pair_name="pair-a",
        symbol="PI_XBTUSD",
        order_id="OID-1",
        client_order_id="CID-1",
        status="PartiallyFilled",
        reason="partial_fill",
        price=None,
        stop_price=None,
        filled_quantity=Decimal("1"),
        total_quantity=Decimal("2"),
        occurred_at=datetime(2026, 5, 21, 12, 0, tzinfo=timezone.utc),
    )

    move = head_move_from_private_fact(fact)

    assert move.kind == EggMoveKind.PLAYED_NOT_CANCELED
    assert move.is_private is True


def test_simulated_private_fill_closes_head() -> None:
    submitted = head_submitted_from_ack(
        pair_name="pair-a",
        symbol="PI_XBTUSD",
        ack=OrderAck(order_id="OID-1", status="New", orig_qty=1.0, side="buy"),
        client_order_id="CID-1",
    )

    move = simulated_private_fill_from_submission(submitted, played_quantity=1)

    assert move.kind == EggMoveKind.PLAYED_AND_CANCELED
    assert move.is_private is True
