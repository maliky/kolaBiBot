from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from sqlalchemy.orm import Session

from kolabi.bot.domain import OrderPairSpec
from kolabi.shared.persistence import OrderEvent, OrderRun, get_sessionmaker


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
