from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict

from sqlalchemy.orm import Session

from kolabi.bot.domain import OrderPairSpec
from kolabi.shared.persistence import OrderEvent, OrderRun, TailTelemetry, get_sessionmaker


@dataclass
class PersistenceConfig:
    db_url: str = "sqlite:///kolabi_bot.db"


class OrderRecorder:
    def __init__(self, config: PersistenceConfig) -> None:
        self.config = config
        self._sessionmaker = get_sessionmaker(config.db_url)

    def start_run(self, pair: OrderPairSpec, indicators: Dict[str, Any]) -> OrderRun:
        session: Session = self._sessionmaker()
        run = OrderRun(
            name=pair.name,
            exchange="",
            symbol="",
            strategy={
                "tps_run": (pair.window.start_minutes, pair.window.end_minutes),
                "prix": pair.head_price,
                "otype": pair.head.order_type,
                "atype": pair.amount_type,
                "indicators": indicators,
            },
            status="submitted",
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        session.close()
        return run

    def record_event(self, run_id: int, event_type: str, status: str, payload: dict) -> None:
        session: Session = self._sessionmaker()
        event = OrderEvent(
            run_id=run_id,
            event_type=event_type,
            status=status,
            payload=payload,
        )
        session.add(event)
        session.commit()
        session.close()


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
    last_tail_update_at: datetime | None
    recorded_at: datetime


class TailTelemetryRecorder:
    def __init__(self, config: PersistenceConfig) -> None:
        self.config = config
        self._sessionmaker = get_sessionmaker(config.db_url)

    def record_rows(self, rows: tuple[TailTelemetryRow, ...]) -> None:
        if not rows:
            return
        session: Session = self._sessionmaker()
        try:
            session.add_all(
                [
                    TailTelemetry(
                        exchange=row.exchange,
                        environment=row.environment,
                        market_type=row.market_type,
                        account_scope=row.account_scope,
                        strategy_id=row.strategy_id,
                        pair_name=row.pair_name,
                        symbol=row.symbol,
                        head_state=row.head_state,
                        tail_state=row.tail_state,
                        tail_mode=row.tail_mode,
                        reference_price=row.reference_price,
                        stop_price=row.stop_price,
                        initial_distance=row.initial_distance,
                        current_distance=row.current_distance,
                        last_tail_update_at=row.last_tail_update_at,
                        recorded_at=row.recorded_at,
                    )
                    for row in rows
                ]
            )
            session.commit()
        finally:
            session.close()
