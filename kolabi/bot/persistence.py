from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict

from sqlalchemy.orm import Session

from kolabi.bot.domain import OrderPairSpec
from kolabi.bot.telemetry import TailTelemetryRow
from kolabi.shared.persistence import (
    OrderEvent,
    OrderRun,
    TailTelemetry,
    get_sessionmaker,
    prune_tail_telemetry,
)
from kolabi.shared.pruning import DEFAULT_PRUNING, TimeCountPruning

_LOGGER = logging.getLogger("kola")


@dataclass
class PersistenceConfig:
    db_url: str
    tail_telemetry_pruning: TimeCountPruning = field(
        default_factory=lambda: DEFAULT_PRUNING.tail_telemetry
    )


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


class TailTelemetryRecorder:
    def __init__(self, config: PersistenceConfig) -> None:
        self.config = config
        self._sessionmaker = get_sessionmaker(config.db_url)
        self._last_tail_prune_monotonic = 0.0

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
            self._prune_tail_telemetry_if_due(session, rows)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _prune_tail_telemetry_if_due(
        self,
        session: Session,
        rows: tuple[TailTelemetryRow, ...],
    ) -> None:
        pruning = self.config.tail_telemetry_pruning
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_tail_prune_monotonic < max(
            1.0,
            pruning.maintenance_seconds,
        ):
            return
        self._last_tail_prune_monotonic = now_monotonic
        lanes = {
            (
                row.exchange,
                row.environment,
                row.market_type,
                row.account_scope,
            )
            for row in rows
        }
        try:
            for exchange, environment, market_type, account_scope in lanes:
                prune_tail_telemetry(
                    session,
                    exchange=exchange,
                    environment=environment,
                    market_type=market_type,
                    account_scope=account_scope,
                    retention_minutes=pruning.retention_minutes,
                    retention_limit=pruning.retention_limit,
                    now=datetime.now(timezone.utc),
                )
            session.commit()
        except Exception as exc:
            session.rollback()
            _LOGGER.warning("tail telemetry pruning skipped error=%s", _compact_error(exc))

def _compact_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())
