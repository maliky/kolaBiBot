from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class TailTelemetryRow:
    exchange: str
    environment: str
    market_type: str
    account_scope: str
    strategy_id: str | None
    pair_name: str
    symbol: str
    head_state: str
    tail_state: str
    tail_mode: str | None
    reference_price: float
    stop_price: float
    initial_distance: float
    current_distance: float
    spread_guard: float
    unblock_requirement: float
    last_tail_update_at: datetime | None
    recorded_at: datetime
