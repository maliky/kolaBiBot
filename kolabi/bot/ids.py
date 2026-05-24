"""Pure identifier helpers for pair-cycle order emissions.

Purpose: derive stable, exchange-safe client identifiers for head and tail
orders emitted by the pair reducer.
Inputs: pair metadata and timestamp.
Outputs: normalized client order identifier strings.
Side effects: none.
Important types: `OrderPairSpec`.
Role: pure logic.
Transitional: yes, extracted from `pair_cycle.py` as part of reducer cleanup.
"""
from __future__ import annotations

from datetime import datetime, timezone

from kolabi.bot.domain import OrderPairSpec


def head_client_order_id(pair: OrderPairSpec, *, at: datetime | None = None) -> str:
    """Build a bounded, exchange-safe client identifier for head submissions."""
    safe_name = "".join(ch for ch in pair.name if ch.isalnum() or ch in {"_", "-"})
    timestamp = at if at is not None else datetime.now(timezone.utc)
    stamp = timestamp.strftime("%Y%m%d%H%M%S")
    return f"kolabi-{safe_name}-head-{stamp}"[:64]


def tail_client_order_id(pair: OrderPairSpec, *, at: datetime | None = None) -> str:
    """Build a bounded, exchange-safe client identifier for tail submissions."""
    safe_name = "".join(ch for ch in pair.name if ch.isalnum() or ch in {"_", "-"})
    timestamp = at if at is not None else datetime.now(timezone.utc)
    stamp = timestamp.strftime("%Y%m%d%H%M%S")
    return f"kolabi-{safe_name}-tail-{stamp}"[:64]
