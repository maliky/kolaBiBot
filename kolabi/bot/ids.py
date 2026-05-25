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

import re
from datetime import datetime, timezone

import coolname

from kolabi.bot.domain import OrderPairSpec


def head_client_order_id(pair: OrderPairSpec, *, at: datetime | None = None) -> str:
    """Build a readable, exchange-safe client identifier for head submissions."""
    del pair
    return _client_order_id("H", at=at)


def tail_client_order_id(pair: OrderPairSpec, *, at: datetime | None = None) -> str:
    """Build a readable, exchange-safe client identifier for tail submissions."""
    del pair
    return _client_order_id("T", at=at)


def _client_order_id(prefix: str, *, at: datetime | None = None) -> str:
    timestamp = at if at is not None else datetime.now(timezone.utc)
    stamp = timestamp.strftime("%y%m%d%H%M%S")
    word = _slug_word()
    candidate = f"{prefix}-{word}-{stamp}".lower()
    safe = re.sub(r"[^a-z0-9-]+", "-", candidate).strip("-")
    return safe[:64]


def _slug_word() -> str:
    slug = coolname.generate_slug(2)
    words = [word for word in slug.split("-") if word]
    if words:
        return words[0]
    return "signal"
